"""本番QAで実測された「顧客名なし依頼」の失敗クラスを CI で捕まえる契約（P1-3 (a)）。

## なぜ別ファイルか

``tests/skills/_shared/test_client_name_guard.py`` はガードの**関数仕様**（正規化・残差法・
演算子拒否）を固定する単体テスト。本ファイルはそれと目的が違い、**2026-08-20 の本番QAで
実際に起きた発話**を出発点に、「その発話からルーターが作りうる client_name が受信箱を
叩く前に止まるか」を固定する。ルーティング・コーパス（tests/routing/）は LLM 依存で
非決定的なので pytest ゲートにできない。**この失敗クラスを CI が決定論的に捕まえられる
唯一の場所がここ**。

## 実測された事故（再掲）

    「今週の空いてる時間を教えて」→ mail_followup(client_name="今週の空き時間") → scanned=0
    「返信が必要なメールを教えて」→ mail_summary(client_name="返信必要")       → scanned=0
    「今日届いたメールを要約して」→ mail_summary(client_name="今日のメール")     → scanned=0

Gmail の完全一致フレーズ検索なので必ず 0 件。利用者には「連携が壊れた」に見えるが連携は正常。

## この契約が保証すること / しないこと

* 保証する: 上の値がガードで ``structural`` になり、案内文が返り、error コードが立つこと。
* 保証しない: **ルーターがそもそも正しいツールを選ぶか**（＝ LLM 判断）。それは
  ``tests/routing/README.md`` の手動シミュ（R4 ラウンド）が担当する。
* 既知の穴は :data:`KNOWN_GAPS` に**実測値ごと**書いてある。ガードを改善したらここが赤くなり、
  「穴が塞がった」ことを人間が表に反映せざるを得ない（穴が黙って残るのも黙って消えるのも防ぐ）。
"""

from __future__ import annotations

import json
from pathlib import Path

from teamagent.skills._shared.client_name_guard import (
    ERROR_BY_VERDICT,
    classify_client_name,
    guard_message,
    non_hiragana_len,
    normalize_client_name,
    residual_of,
)

_CORPUS = Path(__file__).resolve().parents[1] / "routing" / "catalog_routing_corpus.jsonl"


# ルーターが依頼文から作りうる client_name のうち、**ガードが止められるもの**。
# (corpus id, 本番QAの発話, ルーターが詰めうる client_name)
CAUGHT_FABRICATIONS: tuple[tuple[str, str, str], ...] = (
    ("freebusy-01", "今週の空いてる時間を教えて", "今週の空き時間"),
    ("freebusy-01", "今週の空いてる時間を教えて", "今週の空いてる時間"),
    ("freebusy-01", "今週の空いてる時間を教えて", "空いてる時間"),
    ("mailnc-01", "返信が必要なメールを教えて", "返信必要"),
    ("mailnc-01", "返信が必要なメールを教えて", "返信が必要"),
    ("mailnc-01", "返信が必要なメールを教えて", "要返信のメール"),
    ("mailnc-02", "今日届いたメールを要約して", "今日のメール"),
    ("mailnc-03", "未読たまってない？", "未読"),
    ("mailnc-03", "未読たまってない？", "未読メール"),
    ("mailnc-04", "放置してるメールある？", "放置"),
    ("agenda-01", "明日の予定を教えて", "明日の予定"),
    ("agenda-02", "今日のスケジュール一覧", "今日のスケジュール"),
    ("agenda-02", "今日のスケジュール一覧", "スケジュール一覧"),
    ("agenda-03", "今日なにがある？", "今日の予定"),
)

# ルーターが作りうるが **今のガードでは structural にできない** 値（残差法の取りこぼし）。
# 残差が「名前らしい」ため verdict は ok になり、2 本目の検索まで発行される。
# (corpus id, 発話, client_name, 実測の残差)
KNOWN_GAPS: tuple[tuple[str, str, str, str], ...] = (
    ("freebusy-02", "30分のMTGどこに入る？", "30分のMTG", "30分MTG"),
)

# verdict は ok（1 本目は引く）が、**2 本目の検索は武装解除されている**値。
# 2026-08-20 レビュー 要修正1 の実測: 残差が活用の残りかす（している / 届いた）だと、
# 2 本目 ``"している"`` が **無関係な他社のメール**にヒットし、それを元の client_name の
# 要約として error="" / connection="live" のまま返していた（＋Bedrock 課金 1 回）。
# いまは残差に非ひらがな 2 文字以上を要求して 2 本目を出さない＝1 本目の
# 「正直な 0 件」に着地する。**ここが CAUGHT へ移るのは構造語リストに活用形を足した時**。
# (corpus id, 発話, client_name, 実測の残差)
SECOND_STAGE_DISARMED: tuple[tuple[str, str, str, str], ...] = (
    ("mailnc-02", "今日届いたメールを要約して", "今日届いたメール", "届いた"),
    ("mailnc-03", "未読たまってない？", "たまってる未読", "たまってる"),
    ("mailnc-04", "放置してるメールある？", "放置してるメール", "してる"),
    ("mailnc-04", "放置してるメールある？", "放置しているメール", "している"),
)

# ⚠️ 誤爆台帳（KNOWN_GAPS の対）。ガードが **実在しうる社名を殺している** ケースを実測値ごと
# 固定する。これが無いと「安全側に倒した」代償が誰にも見えないまま実顧客が沈黙で死ぬ。
# 判定を緩めたらここが赤くなり、緩和の是非を人間が明示裁定せざるを得なくなる。
# (client_name, 実在しうる根拠, 実測 verdict)
KNOWN_FALSE_POSITIVES: tuple[tuple[str, str, str], ...] = (
    ("明日香", "株式会社明日香（実在の一般的な社名）。『明日』が構造語で残差 1 文字", "structural"),
    ("時間堂", "劇団『時間堂』。『時間』が構造語", "structural"),
    ("一覧堂", "『一覧』が構造語", "structural"),
    ("要約社", "『要約』が構造語", "structural"),
    ("森", "1 文字社名は残差長 1 で落ちる（閾値そのもの）", "structural"),
)

# 全ひらがな社名＋構造語。verdict は ok（1 本目は引く）が 2 本目を失う＝救済が効かない。
# 誤帰属ではなく「0 件でした」に着地するので **安全側の失敗**として許容している。
HIRAGANA_NAMES_LOSING_THE_SECOND_STAGE: tuple[str, ...] = (
    "とらやのメール",
    "はなまるうどんのメール",
)

# 実在するお客様名（構造語を含むもの・全ひらがなも含む）。**絶対に殺してはいけない**対照群。
REAL_CUSTOMER_NAMES: tuple[str, ...] = (
    "花王",
    "アサヒ飲料",
    "森ビル",
    "日本メール便",  # 「メール」を含むが残差「日本便」が残る
    "花王のメール",  # 二段検索の 2 本目で「花王」に落ちる
    "とらや",  # 全ひらがな（助詞削除で消えてはいけない）
    "はなまるうどん",
    "(株)ABC",
)


def _corpus_ids() -> set[str]:
    rows = [
        json.loads(line)
        for line in _CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {row["id"] for row in rows}


def test_measured_production_fabrications_are_rejected_before_touching_gmail() -> None:
    """実測事故の値が structural 判定になり、検索キーワードが 1 本も作られないこと。"""
    assert CAUGHT_FABRICATIONS, "検出器そのものが空（vacuous green）"
    for corpus_id, utterance, fabricated in CAUGHT_FABRICATIONS:
        verdict = classify_client_name(fabricated)
        assert verdict.verdict == "structural", (
            f"{corpus_id}『{utterance}』→ client_name={fabricated!r} が "
            f"{verdict.verdict}（reason={verdict.reason}）。受信箱を叩いて 0 件になる"
        )
        # search_terms が空＝ Gmail クエリを 1 本も組めない（構造的に検索できない）。
        assert verdict.search_terms == []


def test_rejection_tells_the_user_the_connection_is_fine() -> None:
    """0 件を「連携が壊れた」と誤解させない案内文と、機械可読な error コードが返ること。"""
    for _corpus_id, _utterance, fabricated in CAUGHT_FABRICATIONS:
        verdict = classify_client_name(fabricated)
        message = guard_message(verdict)
        assert "連携は正常です" in message
        assert "お客様" in message
        assert ERROR_BY_VERDICT[verdict.verdict] == "client_name_structural"


def test_known_gaps_are_recorded_with_their_measured_residual() -> None:
    """取りこぼしを**実測値ごと**固定する。塞いだら赤くなるので表の更新を強制できる。"""
    for corpus_id, utterance, fabricated, residual in KNOWN_GAPS:
        normalized = normalize_client_name(fabricated)
        assert residual_of(normalized) == residual, (
            f"{corpus_id}『{utterance}』→ {fabricated!r} の残差が {residual!r} から変わった。"
            "ガードを改善したなら KNOWN_GAPS から CAUGHT_FABRICATIONS へ移すこと"
        )
        assert classify_client_name(fabricated).verdict == "ok"


def test_disarmed_rows_never_issue_a_second_query() -> None:
    """残差が活用の残りかすの行は、**2 本目の検索キーワードを 1 本も作らない**。

    ここが緑であることが「無関係な他社メールを client_name の要約として返さない」の
    決定的な担保。verdict は ok のままなので 1 本目（原文フレーズ）は引き、0 件なら
    「連携は正常です／0 件でした」という**正直な 0 件**に着地する。
    """
    assert SECOND_STAGE_DISARMED, "検出器そのものが空（vacuous green）"
    for corpus_id, utterance, fabricated, residual in SECOND_STAGE_DISARMED:
        normalized = normalize_client_name(fabricated)
        assert residual_of(normalized) == residual, (
            f"{corpus_id}『{utterance}』→ {fabricated!r} の残差が {residual!r} から変わった"
        )
        verdict = classify_client_name(fabricated)
        assert verdict.verdict == "ok"
        assert verdict.search_terms == [normalized], (
            f"{corpus_id}『{utterance}』→ {fabricated!r} が残差 {residual!r} で受信箱を"
            f"引き直そうとしている（無関係なメールを掴む）: {verdict}"
        )
        assert non_hiragana_len(residual) < 2


def test_known_false_positives_are_recorded_as_the_price_of_the_guard() -> None:
    """安全側に倒した代償（実在しうる社名を殺す）を台帳として残す。

    緩めるにせよ据え置くにせよ、**気づかないまま**にしないことが目的。
    """
    assert KNOWN_FALSE_POSITIVES, "誤爆台帳が空（代償を測っていない）"
    for name, why, expected in KNOWN_FALSE_POSITIVES:
        verdict = classify_client_name(name)
        assert verdict.verdict == expected, (
            f"{name!r}（{why}）の判定が {expected} から {verdict.verdict} に変わった。"
            "ガードを緩めた/締めたなら KNOWN_FALSE_POSITIVES を更新すること"
        )


def test_hiragana_names_lose_only_the_second_stage_not_the_search() -> None:
    """全ひらがな社名＋構造語は 2 本目を失うが、1 本目は必ず引く（黙って殺さない）。"""
    for name in HIRAGANA_NAMES_LOSING_THE_SECOND_STAGE:
        verdict = classify_client_name(name)
        assert verdict.verdict == "ok", f"{name!r} を structural にしてはいけない"
        assert verdict.search_terms == [normalize_client_name(name)]


def test_real_customer_names_are_never_killed_by_the_guard() -> None:
    """構造語を含む/全ひらがなの実在社名を殺していないこと（ガード強化時の安全弁）。"""
    for name in REAL_CUSTOMER_NAMES:
        verdict = classify_client_name(name)
        assert verdict.verdict == "ok", f"実在しうるお客様名 {name!r} を弾いている"
        assert verdict.search_terms, f"{name!r} の検索キーワードが空"
        assert guard_message(verdict) == ""


def test_gmail_operator_injection_is_refused() -> None:
    """client_name 経由の Gmail 検索演算子持ち込みを拒否する（受信箱の越境防止）。"""
    for payload in ('x" OR from:ceo@example.com "', "label:重要", "花王：至急"):
        verdict = classify_client_name(payload)
        assert verdict.verdict == "structural"
        assert verdict.reason == "operator_colon"
        assert verdict.search_terms == []


def test_every_fabrication_is_anchored_to_a_routing_corpus_row() -> None:
    """本ファイルの発話が corpus から消えたら赤にする（2 つの成果物を接着する）。"""
    ids = _corpus_ids()
    referenced = (
        {cid for cid, _u, _v in CAUGHT_FABRICATIONS}
        | {cid for cid, _u, _v, _r in KNOWN_GAPS}
        | {cid for cid, _u, _v, _r in SECOND_STAGE_DISARMED}
    )
    missing = sorted(referenced - ids)
    assert not missing, f"corpus に無い id を参照している: {missing}"
