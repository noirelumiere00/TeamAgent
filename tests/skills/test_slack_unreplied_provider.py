"""SlackUnrepliedProvider（Slack 返信漏れ検知・v0.3 Task1）の単体テスト。

外部 I/O 無し（store / reader ともフェイク）。判定ロジックの境界を攻める:
未連携・scope欠落・未返信検出・返信済み除外・自己発言スキップ・root去重・
permalink の thread_ts 解決・API失敗時の skip（証拠なしに未返信と言わない）・上限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from teamagent.adapters.slack_channel_ingest_client import SlackMessage
from teamagent.adapters.slack_user_reader import SlackSearchMatch
from teamagent.skills._shared.slack_unreplied import (
    SlackUnrepliedProvider,
    _channel_kind,
)

_UID = "U_ME"


@dataclass
class _Tok:
    access_token: str = "xoxp-test"
    scopes: tuple[str, ...] = ("search:read", "channels:history")
    slack_user_id: str = _UID


class _Store:
    def __init__(self, tok: Any) -> None:
        self._tok = tok

    def get(self, user_email: str) -> Any:
        return self._tok


@dataclass
class _FakeReader:
    matches: list[SlackSearchMatch] = field(default_factory=list)
    threads: dict[tuple[str, str], list[SlackMessage]] = field(default_factory=dict)
    search_queries: list[str] = field(default_factory=list)
    thread_calls: list[tuple[str, str]] = field(default_factory=list)

    def search(self, query: str, request_id: str, *, count: int = 15) -> list[SlackSearchMatch]:
        self.search_queries.append(query)
        return self.matches[:count]

    def read_thread(
        self, channel_id: str, thread_ts: str, request_id: str, *, limit: int = 200
    ) -> list[SlackMessage]:
        self.thread_calls.append((channel_id, thread_ts))
        return self.threads.get((channel_id, thread_ts), [])


def _provider(reader: _FakeReader, tok: Any = None, **kw: Any) -> SlackUnrepliedProvider:
    return SlackUnrepliedProvider(
        slack_store=_Store(tok if tok is not None else _Tok()),
        reader_factory=lambda _xoxp: reader,
        **kw,
    )


def _match(
    ts: str = "1000.1", ch: str = "C1", user: str = "U_OTHER", **kw: Any
) -> SlackSearchMatch:
    return SlackSearchMatch(
        ts=ts,
        text=kw.get("text", f"<@{_UID}> 確認お願いします"),
        channel_id=ch,
        channel_name=kw.get("channel_name", "sales-acme"),
        user=user,
        permalink=kw.get("permalink", f"https://x.slack.com/archives/{ch}/p{ts.replace('.', '')}"),
    )


def _msg(ts: str, user: str | None) -> SlackMessage:
    return SlackMessage(ts=ts, user=user, text="…")


@dataclass
class _NamingReader(_FakeReader):
    """実名解決ができる reader（users.info 相当）。names に無い ID は None を返す。"""

    names: dict[str, str | None] = field(default_factory=dict)
    name_calls: list[str] = field(default_factory=list)
    raises: bool = False

    def get_display_name(self, user_id: str, request_id: str) -> str | None:
        self.name_calls.append(user_id)
        if self.raises:
            raise RuntimeError("users.info down")
        return self.names.get(user_id)


def test_no_token_returns_empty() -> None:
    p = _provider(_FakeReader(), tok=None)
    p._store = _Store(None)  # 未連携
    assert p.collect("a@b.co", 7, "r1") == []


def test_missing_search_scope_returns_empty() -> None:
    reader = _FakeReader(matches=[_match()])
    p = _provider(reader, tok=_Tok(scopes=("channels:history",)))  # 旧スコープ連携
    assert p.collect("a@b.co", 7, "r1") == []
    assert reader.search_queries == []  # API を叩かない


def test_fail_open_paths_are_marked_as_not_scanned() -> None:
    """🔴 fail-open の空リストと「本当に 0 件」を **区別できる事実** を返す。

    未連携・旧 scope・store 障害・reader 生成失敗はすべて空の Collection に潰れる。
    scanned=False が無いと、下流は毎朝「Slack 返信漏れ: なし」と嘘をつく（＝この機能が
    潰そうとしている見逃しそのもの）。
    """

    class _BoomStore:
        def get(self, user_email: str) -> Any:
            raise RuntimeError("db down")

    def _boom_factory(_xoxp: str) -> Any:
        raise RuntimeError("xoxp empty")

    no_token = _provider(_FakeReader(), tok=None)
    no_token._store = _Store(None)
    old_scope = _provider(_FakeReader(matches=[_match()]), tok=_Tok(scopes=("channels:history",)))
    store_down = SlackUnrepliedProvider(
        slack_store=_BoomStore(), reader_factory=lambda _x: _FakeReader()
    )
    reader_down = SlackUnrepliedProvider(slack_store=_Store(_Tok()), reader_factory=_boom_factory)
    for p in (no_token, old_scope, store_down, reader_down):
        r = p.collect_detailed("a@b.co", 7, "r1")
        assert r.items == () and r.total_unreplied == 0
        assert r.scanned is False, p


def test_truly_zero_results_are_marked_as_scanned() -> None:
    """走査できて 0 件だったときだけ scanned=True（「なし」と言い切ってよい根拠）。"""
    r = _provider(_FakeReader(matches=[])).collect_detailed("a@b.co", 7, "r1")
    assert (r.items, r.total_unreplied, r.scanned) == ((), 0, True)


def test_unreplied_mention_detected_and_mapped() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER")]},  # 自分の返信なし
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert len(out) == 1
    m = out[0]
    assert m.channel_name == "sales-acme"
    assert m.permalink.startswith("https://")
    assert m.occurred_at.startswith("1970-01-01T09:16")  # epoch1000秒 = JST 09:16:40
    # 検索クエリは本人メンション＋期間絞り。after: は排他的（日付単位）なので
    # horizon ちょうど前の日を含めるため horizon+1 日遡った日付になる。
    from datetime import datetime, timedelta, timezone

    q = reader.search_queries[0]
    assert f"<@{_UID}>" in q
    expected = (
        (datetime.now(tz=timezone(timedelta(hours=9))) - timedelta(days=8)).date().isoformat()
    )
    assert f"after:{expected}" in q


def test_replied_after_mention_excluded() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER"), _msg("1500.0", _UID)]},
    )
    assert _provider(reader).collect("a@b.co", 7, "r1") == []


def test_reply_before_mention_does_not_count() -> None:
    # メンションより前の自分の発言は「返信」ではない（同スレで過去に発言済みでも検知する）。
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("900.0", _UID), _msg("1000.1", "U_OTHER")]},
    )
    assert len(_provider(reader).collect("a@b.co", 7, "r1")) == 1


def test_self_authored_match_skipped_without_thread_check() -> None:
    reader = _FakeReader(matches=[_match(user=_UID)])
    assert _provider(reader).collect("a@b.co", 7, "r1") == []
    assert reader.thread_calls == []  # replies を浪費しない


def test_dedup_by_thread_root() -> None:
    # 同一スレッドの2メンション → replies 照会は1回・結果も1件。
    pl = "https://x.slack.com/archives/C1/p10002?thread_ts=1000.1&cid=C1"
    reader = _FakeReader(
        matches=[_match(ts="1000.2", permalink=pl), _match(ts="1000.3", permalink=pl)],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER")]},
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert len(out) == 1
    assert reader.thread_calls == [("C1", "1000.1")]  # permalink の thread_ts が root


def test_thread_read_failure_skips_item() -> None:
    # replies が空（fail-open＝API失敗）→ 判定不能なので「未返信」と主張しない。
    reader = _FakeReader(matches=[_match(ts="1000.1")], threads={})
    assert _provider(reader).collect("a@b.co", 7, "r1") == []


def test_max_items_cap() -> None:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}") for i in range(8)]
    threads = {(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", "U_OTHER")] for i in range(8)}
    out = _provider(_FakeReader(matches=matches, threads=threads), max_items=3).collect(
        "a@b.co", 7, "r1"
    )
    assert len(out) == 3


def test_max_thread_checks_cap() -> None:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}") for i in range(8)]
    reader = _FakeReader(matches=matches, threads={})  # 全部判定不能でも
    _provider(reader, max_thread_checks=4).collect("a@b.co", 7, "r1")
    assert len(reader.thread_calls) == 4  # replies 呼び出しは上限で打ち切り


def test_store_failure_fail_open() -> None:
    class _BoomStore:
        def get(self, user_email: str) -> Any:
            raise RuntimeError("db down")

    p = SlackUnrepliedProvider(slack_store=_BoomStore(), reader_factory=lambda _x: _FakeReader())
    assert p.collect("a@b.co", 7, "r1") == []


# ── 母数（総件数）と走査の完全性 ─────────────────────────────────────────


def _n_matches(n: int) -> tuple[list[SlackSearchMatch], dict[tuple[str, str], list[SlackMessage]]]:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}") for i in range(n)]
    threads = {(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", "U_OTHER")] for i in range(n)}
    return matches, threads


def test_all_candidates_reach_the_judgement_layer() -> None:
    """collect_detailed は **判定できた全件** を返す（表示件数で間引かない）。

    ⚠️ ここで max_items まで削ると「新しい順の先頭 N 件」しか判定層に届かず、
    その先にある「あなたの番」が DM に一切現れない（母数だけが増える）。
    表示件数を決めるのは描画層（_HANDOFF_MAX_ITEMS）の仕事。
    """
    matches, threads = _n_matches(8)
    reader = _FakeReader(matches=matches, threads=threads)
    r = _provider(reader, max_items=3, max_thread_checks=20).collect_detailed("a@b.co", 7, "r1")
    assert len(r.items) == 8
    assert r.total_unreplied == 8  # 表示上限を超えて数える
    assert r.thread_checks == 8  # 3 件目で走査を止めていない
    assert r.scanned_matches == 8
    assert r.undetermined == 0
    assert r.scan_truncated is False


def test_collect_still_returns_capped_list() -> None:
    """後方互換 API（collect）は従来どおり max_items 件のリスト。"""
    matches, threads = _n_matches(8)
    out = _provider(
        _FakeReader(matches=matches, threads=threads), max_items=3, max_thread_checks=20
    ).collect("a@b.co", 7, "r1")
    assert isinstance(out, list) and len(out) == 3


def test_scan_truncated_when_thread_check_cap_hit() -> None:
    """打ち切ったら母数は下限値。フラグで正直に申告する。"""
    matches, threads = _n_matches(8)
    r = _provider(
        _FakeReader(matches=matches, threads=threads), max_items=10, max_thread_checks=4
    ).collect_detailed("a@b.co", 7, "r1")
    assert r.thread_checks == 4
    assert r.total_unreplied == 4
    assert r.scan_truncated is True


def test_scan_truncated_when_search_hits_its_own_cap() -> None:
    matches, threads = _n_matches(3)
    r = _provider(
        _FakeReader(matches=matches, threads=threads), search_count=3, max_thread_checks=20
    ).collect_detailed("a@b.co", 7, "r1")
    assert r.scanned_matches == 3
    assert r.scan_truncated is True  # search が頭打ち＝この先は見えていない


def test_undetermined_counted_separately() -> None:
    """replies が取れなかった候補は未返信に数えない（証拠なしに主張しない）。"""
    matches, _ = _n_matches(3)
    r = _provider(_FakeReader(matches=matches, threads={}), max_thread_checks=20).collect_detailed(
        "a@b.co", 7, "r1"
    )
    assert r.total_unreplied == 0
    assert r.undetermined == 3


def test_empty_collection_on_missing_scope() -> None:
    r = _provider(_FakeReader(matches=[_match()]), tok=_Tok(scopes=())).collect_detailed(
        "a@b.co", 7, "r1"
    )
    assert r == type(r)()  # すべて既定値（items 空・母数 0・打ち切りなし）


# ── 会話種別（channel_id の先頭1文字・API 追加呼び出し 0 回） ────────────────


def test_channel_kind_from_id_prefix() -> None:
    assert _channel_kind("D01ABCDEF") == "dm"
    assert _channel_kind("G01ABCDEF") == "group_dm"
    assert _channel_kind("C01ABCDEF") == "channel"
    assert _channel_kind("") == "unknown"
    assert _channel_kind("X01ABCDEF") == "unknown"  # 判定できない＝埋めない


def test_channel_kind_prefers_mpdm_name_over_prefix() -> None:
    # 新しめの WS では複数人 DM が C 始まりのことがある。name が真実に近い。
    assert _channel_kind("C01ABCDEF", "mpdm-alice--bob--carol-1") == "group_dm"


def test_channel_kind_flows_into_mentions() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1", ch="D1")],
        threads={("D1", "1000.1"): [_msg("1000.1", "U_OTHER")]},
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert out[0].channel_kind == "dm"
    assert out[0].channel_id == "D1"
    assert reader.thread_calls == [("D1", "1000.1")]  # 種別判定に API を足していない


# ── スレッド由来の文脈（追加 API 呼び出し 0 回） ────────────────────────────


def test_thread_context_extracted_without_extra_api() -> None:
    # 名指し抽出は実 ID 形式（大文字英数）でだけ拾う。他テストの擬似 ID（U_ME 等）は
    # 形式が違うので拾わない＝ここだけ本物に寄せる。
    reader = _FakeReader(
        matches=[
            _match(ts="1000.1", user="U_BOSS", text="<@U01ABCDEF> <@U02GHIJKL|bob> 確認お願い")
        ],
        threads={
            ("C1", "1000.1"): [
                _msg("1000.1", "U_BOSS"),
                _msg("1200.0", "U_OTHER2"),  # 他人が後から発言
                _msg("1300.0", None),  # bot 投稿（user なし）でも落ちない
            ]
        },
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.user == "U_BOSS"
    assert m.thread_message_count == 3
    assert m.thread_participant_ids == ("U_BOSS", "U_OTHER2")
    assert m.thread_last_user_id is None  # 最終発言は bot
    assert m.thread_last_at.startswith("1970-01-01T09:21")  # epoch1300 = JST 09:21:40
    assert m.answered_by_other is True
    assert m.sender_followed_up is False
    assert m.mentioned_user_ids == ("U01ABCDEF", "U02GHIJKL")
    assert len(reader.thread_calls) == 1  # replies は 1 回のまま


def test_sender_followup_is_not_counted_as_answered_by_other() -> None:
    """差出人の催促を「他人が代わりに答えた」と読み違えない（畳む判断を誤らせない）。"""
    reader = _FakeReader(
        matches=[_match(ts="1000.1", user="U_BOSS")],
        threads={
            ("C1", "1000.1"): [_msg("1000.1", "U_BOSS"), _msg("1400.0", "U_BOSS")],
        },
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.answered_by_other is False
    assert m.sender_followed_up is True


def test_answered_by_other_false_when_nobody_spoke_after() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("900.0", "U_X"), _msg("1000.1", "U_OTHER")]},
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.answered_by_other is False
    assert m.sender_followed_up is False
    assert m.thread_last_user_id == "U_OTHER"


# ── 差出人の実名解決 ────────────────────────────────────────────────────────


def test_display_name_resolved_for_shown_items() -> None:
    reader = _NamingReader(
        matches=[_match(ts="1000.1", user="U_BOSS")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_BOSS")]},
        names={"U_BOSS": "山田 太郎"},
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.user_display == "山田 太郎"
    assert reader.name_calls == ["U_BOSS"]


def test_display_name_none_when_unresolved() -> None:
    """解決できなければ None のまま＝架空の名前を作らない。"""
    reader = _NamingReader(
        matches=[_match(ts="1000.1", user="U_GHOST")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_GHOST")]},
        names={},
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.user is not None and m.user_display is None


def test_display_name_lookup_failure_is_fail_open() -> None:
    reader = _NamingReader(
        matches=[_match(ts="1000.1", user="U_BOSS")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_BOSS")]},
        raises=True,
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert len(out) == 1 and out[0].user_display is None  # 件自体は消えない


def test_display_name_lookups_are_deduped_and_capped() -> None:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}", user="U_BOSS") for i in range(4)]
    threads = {(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", "U_BOSS")] for i in range(4)}
    reader = _NamingReader(matches=matches, threads=threads, names={"U_BOSS": "山田"})
    out = _provider(reader, max_thread_checks=20).collect("a@b.co", 7, "r1")
    assert len(out) == 4
    assert reader.name_calls == ["U_BOSS"]  # 同一差出人は 1 回だけ

    reader2 = _NamingReader(
        matches=[_match(ts=f"{1000 + i}.1", ch=f"C{i}", user=f"U{i}") for i in range(4)],
        threads={(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", f"U{i}")] for i in range(4)},
        names={},
    )
    _provider(reader2, max_thread_checks=20, max_name_lookups=2).collect("a@b.co", 7, "r1")
    assert len(reader2.name_calls) == 2  # 上限で打ち切る


def test_display_name_lookups_are_bounded_by_their_own_cap() -> None:
    """users.info を浪費しない上限は **max_name_lookups**（表示件数ではない）。

    判定層へ全件渡すようになったので、実名解決の上限もこちらが唯一の歯止め。
    """
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}", user=f"U{i}") for i in range(6)]
    threads = {(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", f"U{i}")] for i in range(6)}
    reader = _NamingReader(matches=matches, threads=threads, names={})
    _provider(reader, max_items=2, max_thread_checks=20, max_name_lookups=3).collect(
        "a@b.co", 7, "r1"
    )
    assert len(reader.name_calls) == 3


def test_legacy_reader_without_get_display_name_still_works() -> None:
    """旧 adapter（get_display_name 無し）でも落ちない＝機能が無かった時と同じ挙動。"""
    reader = _FakeReader(
        matches=[_match(ts="1000.1", user="U_BOSS")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_BOSS")]},
    )
    m = _provider(reader).collect("a@b.co", 7, "r1")[0]
    assert m.user_display is None


def test_full_body_is_not_truncated_by_provider() -> None:
    """本文の切り詰めは描画側の責務。データ層は原文をそのまま持ち帰る。"""
    body = "あ" * 1600
    reader = _FakeReader(
        matches=[_match(ts="1000.1", text=body)],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER")]},
    )
    assert _provider(reader).collect("a@b.co", 7, "r1")[0].text == body


def test_mentioned_ids_only_match_real_slack_id_format() -> None:
    from teamagent.skills._shared.slack_unreplied import _mentioned_ids

    assert _mentioned_ids("<@U01ABCDEF> と <@W02GHIJKL|alice> と <@U01ABCDEF>") == (
        "U01ABCDEF",
        "W02GHIJKL",
    )  # 登場順・重複排除
    assert _mentioned_ids("<!here> <#C01ABCDEF|general> 通常テキスト") == ()
    assert _mentioned_ids("") == ()
