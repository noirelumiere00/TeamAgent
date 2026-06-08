"""skills/intent.py の自動ルーティング判定テスト。"""

from __future__ import annotations

import pytest

from teamagent.skills.intent import detect_skill, extract_search_topic, extract_video_url


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("飲食店のPR事例を教えて", "飲食店のPR事例"),
        ("新宿のランチ事例を調べて", "新宿のランチ事例"),
        ("過去の美容案件まとめて", "過去の美容案件"),
        ("マンダムの前回提案は？", "マンダムの前回提案"),
        ("競合の提案資料ある？", "競合の提案資料"),
        ("", None),  # 空はフォールバック
        ("   ", None),  # 空白のみ
        ("あ", None),  # 1文字は弱いので採らない
    ],
)
def test_extract_search_topic(msg: str, expected: str | None) -> None:
    """検索 ack の話題復唱用: 末尾の依頼語を削いで本題だけ返す。空/短すぎは None。"""
    assert extract_search_topic(msg) == expected


def test_extract_search_topic_long_query_falls_back() -> None:
    """長すぎるクエリは None（崩れた復唱を出さずに汎用 ack へフォールバック）。"""
    assert extract_search_topic("あ" * 50) is None


@pytest.mark.parametrize(
    "msg,expected_url",
    [
        ("この動画分析して https://youtube.com/shorts/abc123", "https://youtube.com/shorts/abc123"),
        ("<https://youtu.be/xYz>", "https://youtu.be/xYz"),
        ("競合 https://www.tiktok.com/@u/video/123 を見て", "https://www.tiktok.com/@u/video/123"),
        ("普通の質問です", None),
    ],
)
def test_extract_video_url(msg: str, expected_url: str | None) -> None:
    assert extract_video_url(msg) == expected_url


@pytest.mark.parametrize(
    "msg",
    [
        "この動画分析して https://youtube.com/shorts/abc123",
        "https://youtu.be/xYz これどう？",
        "https://www.instagram.com/reel/abc/ の構成教えて",
    ],
)
def test_routes_to_video_analysis(msg: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "video_analysis"
    assert intent.video_url is not None


@pytest.mark.parametrize(
    "msg",
    [
        "日本ガイシの提案を作って",
        "コスメブランドのTikTok提案のドラフトちょうだい",
        "飲食チェーンの提案骨子を考えて",
        "この案件のたたき台作って",
        "マンダム向けにどう提案すればいい？",
    ],
)
def test_routes_to_proposal_draft(msg: str) -> None:
    assert detect_skill(msg).skill == "proposal_draft"


@pytest.mark.parametrize(
    "msg",
    [
        "この提案レビューして：飲食チェーン向けTikTok…",
        "提案を添削してほしい",
        "この提案の診断おねがい",
        "提案をブラッシュアップして",
    ],
)
def test_routes_to_proposal_review(msg: str) -> None:
    assert detect_skill(msg).skill == "proposal_review"


@pytest.mark.parametrize(
    "msg,client",
    [
        ("日本ガイシのカルテ", "日本ガイシ"),
        ("マンダムの状況教えて", "マンダム"),
        ("日本ガイシって今どう？", "日本ガイシ"),
        ("サントリーの近況は？", "サントリー"),
        ("東芝の温度感どんな感じ", "東芝"),
    ],
)
def test_routes_to_clientkarte_with_client(msg: str, client: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "clientkarte"
    assert intent.client_name == client


@pytest.mark.parametrize(
    "msg",
    [
        "飲食店のPR事例を教えて",
        "BtoBで刺さった訴求は？",
        "過去のショート動画提案でうまくいったもの",
        "UGC施策の成功例",
    ],
)
def test_routes_to_search_by_default(msg: str) -> None:
    assert detect_skill(msg).skill == "search"


def test_karte_trigger_without_client_falls_back_to_search() -> None:
    """『状況』だけでクライアント名が無いものは search に倒す (誤爆防止)。"""
    assert detect_skill("状況を教えて").skill == "search"


def test_draft_takes_precedence_over_karte() -> None:
    """提案作成意図はカルテより優先。"""
    assert detect_skill("マンダムの状況を踏まえて提案を作って").skill == "proposal_draft"


def test_extract_video_urls_multiple() -> None:
    from teamagent.skills.intent import extract_video_urls

    msg = (
        "競合まとめ\n"
        "https://www.tiktok.com/@a/video/1\n"
        "https://www.tiktok.com/@b/video/2\n"
        "https://youtu.be/xyz"
    )
    urls = extract_video_urls(msg)
    assert len(urls) == 3
    assert "https://www.tiktok.com/@a/video/1" in urls


def test_extract_video_urls_dedupes_and_caps() -> None:
    from teamagent.skills.intent import extract_video_urls

    msg = " ".join(["https://youtu.be/x"] * 5)  # 同一 URL × 5
    assert extract_video_urls(msg) == ["https://youtu.be/x"]
    many = " ".join(f"https://www.tiktok.com/@u/video/{i}" for i in range(30))
    assert len(extract_video_urls(many, limit=20)) == 20


def test_detect_skill_multi_url_sets_video_urls() -> None:
    intent = detect_skill("この2本 https://youtu.be/a https://www.tiktok.com/@u/video/2 分析して")
    assert intent.skill == "video_analysis"
    assert len(intent.video_urls) == 2


@pytest.mark.parametrize(
    "msg,query,stype",
    [
        ("TikTokで新宿 ランチ で検索して", "新宿 ランチ", "keyword"),
        ("tiktokで新宿ランチ検索", "新宿ランチ", "keyword"),
        ("ティックトックで日焼け止め調べて", "日焼け止め", "keyword"),
        ("TikTokで新宿 ランチ をリサーチして", "新宿 ランチ", "keyword"),
        ("#新宿 で調べて", "新宿", "hashtag"),
        ("#日焼け止め 検索して", "日焼け止め", "hashtag"),
    ],
)
def test_routes_to_tiktok_search(msg: str, query: str, stype: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "tiktok_search"
    assert intent.query == query
    assert intent.search_type == stype


@pytest.mark.parametrize(
    "msg",
    [
        # TikTok 言及があっても検索動詞が無ければ search/動画分析に倒す
        "新宿のランチについて教えて",
        "飲食店のPR事例を教えて",
        # ハッシュタグだけ (検索動詞なし) は誤爆させない
        "#新宿 のカフェ いいよね",
    ],
)
def test_tiktok_search_not_overtriggered(msg: str) -> None:
    assert detect_skill(msg).skill != "tiktok_search"


def test_tiktok_url_still_routes_to_video_analysis() -> None:
    """TikTok の動画 URL は検索ではなく動画分析へ (URL が最優先)。"""
    intent = detect_skill("https://www.tiktok.com/@u/video/123 を分析して")
    assert intent.skill == "video_analysis"


# -----------------------------------------------------------
# video_approval (動画一次FB審査) の自動ルーティング
# -----------------------------------------------------------
@pytest.mark.parametrize(
    "msg,mno",
    [
        ("動画チェック E01-01", "E01-01"),
        ("E01-01 の動画チェックして", "E01-01"),
        ("E01-01の動画を確認して", "E01-01"),  # 助詞が直後でも管理番号を拾う
        ("納品チェック 1-1", "1-1"),
        ("E08-02-c の一次FBお願い", "E08-02-c"),
        ("テロップチェックして E01-02", "E01-02"),
    ],
)
def test_routes_to_video_approval(msg: str, mno: str) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "video_approval"
    assert intent.management_no == mno


def test_video_approval_extracts_sheet_id() -> None:
    intent = detect_skill(
        "動画チェック E01-01 "
        "https://docs.google.com/spreadsheets/d/1Og-679JNTc-ecYyw27u6yBRGaaEq1N6y9fZX2wXuyvY/edit#gid=0"
    )
    assert intent.skill == "video_approval"
    assert intent.sheet_id == "1Og-679JNTc-ecYyw27u6yBRGaaEq1N6y9fZX2wXuyvY"
    assert intent.management_no == "E01-01"


def test_video_approval_keyword_without_number_still_routes() -> None:
    """キーワードはあるが管理番号が無い → video_approval に倒し、番号 None (案内文を返す)。"""
    intent = detect_skill("動画チェックお願いします")
    assert intent.skill == "video_approval"
    assert intent.management_no is None


def test_plain_check_still_routes_to_review() -> None:
    """「動画/納品」を伴わない「チェックして」は従来通り proposal_review。"""
    assert detect_skill("この提案チェックして").skill == "proposal_review"


def test_bare_management_no_without_keyword_not_video_approval() -> None:
    """管理番号だけ・動画文脈なしは video_approval に誤爆させない。"""
    assert detect_skill("E01-01 ってどうなってる").skill != "video_approval"


# -----------------------------------------------------------
# video_algorithm (VSEO 動画アルゴリズム読み解き) の自動ルーティング
# -----------------------------------------------------------
@pytest.mark.parametrize(
    "msg,expect_terms",
    [
        ("VSEO分析 新宿 ランチ", ["新宿", "ランチ"]),
        ("新宿ランチをアルゴリズム分析して", ["新宿"]),
        ("TikTokでアルゴリズム分析 日焼け止め", ["日焼け止め"]),
        ("検索上位を分析して 渋谷 カフェ", ["渋谷", "カフェ"]),
    ],
)
def test_routes_to_video_algorithm(msg: str, expect_terms: list[str]) -> None:
    intent = detect_skill(msg)
    assert intent.skill == "video_algorithm"
    assert intent.query is not None
    for t in expect_terms:
        assert t in intent.query


def test_video_algorithm_beats_tiktok_search() -> None:
    """『アルゴリズム分析』は tiktok_search(検索/調べ) ではなく video_algorithm。"""
    assert detect_skill("TikTokで日焼け止めをアルゴリズム分析して").skill == "video_algorithm"


def test_plain_tiktok_search_not_video_algorithm() -> None:
    """『TikTokで○○検索して』は従来通り tiktok_search（分析ではない）。"""
    assert detect_skill("TikTokで新宿ランチ検索して").skill == "tiktok_search"


# --- 雑談ルーティング（Hello 等を検索に流さず会話へ） ---
@pytest.mark.parametrize(
    "msg",
    [
        "Hello",
        "こんにちは",
        "おはよう！",
        "お疲れさまです",
        "ありがとう！",
        "ありがとうございます",
        "あざす",
        "OK",
        "了解です",
        "なるほど",
        "👍",
        "🙏",
        "何ができる？",
        "使い方を教えて",
        "ヘルプ",
        "できること教えて",
    ],
)
def test_routes_to_chitchat(msg: str) -> None:
    """挨拶・お礼・相槌・能力質問は chitchat（検索に流さない）。"""
    assert detect_skill(msg).skill == "chitchat"


@pytest.mark.parametrize(
    "msg",
    [
        "A社",  # 社名のみの検索意図を雑談に奪わない
        "新宿のランチ",
        "ありがとう、A社の提案見せて",  # お礼+タスク混在 → タスク優先
        "VSEO分析して",
        "日本ガイシの状況",
        "飲食店のPR事例を教えて",
        "提案作って",
    ],
)
def test_task_not_misrouted_to_chitchat(msg: str) -> None:
    """業務語/社名を含む入力は chitchat に誤分類されない（task-first・実検索を壊さない）。"""
    assert detect_skill(msg).skill != "chitchat"


# ── mail_to_internal_context / mail_followup ルーティング（Mail×Slack リリース） ──


@pytest.mark.parametrize(
    "msg,client",
    [
        ("森ビルからのこのメール、社内の関連スレッド出して", "森ビル"),
        ("マンダムのメール、社内で何か話してた?", "マンダム"),
        ("INPEXのメール、関連する過去提案ある?", "INPEX"),
        ("花王のメールに触れてる社内スレッドある?", "花王"),
    ],
)
def test_routes_to_mail_to_internal_context(msg: str, client: str) -> None:
    """メール×社内ナレッジ横断: メール語と社内/関連語の近接で発火し、client を抽出する。"""
    it = detect_skill(msg)
    assert it.skill == "mail_to_internal_context"
    assert it.client_name == client


@pytest.mark.parametrize(
    "msg,client,days",
    [
        ("森ビルの要返信メール教えて", "森ビル", None),
        ("A社で要返信のメールある?", "A社", None),
        ("3日以上 返信してない花王のメールある?", "花王", 3),
        ("マンダムの未返信メール教えて", "マンダム", None),
    ],
)
def test_routes_to_mail_followup(msg: str, client: str, days: int | None) -> None:
    """要返信トリアージ: 滞留語で発火し、client と（あれば）日数を抽出する。"""
    it = detect_skill(msg)
    assert it.skill == "mail_followup"
    assert it.client_name == client
    assert it.followup_days == days


@pytest.mark.parametrize(
    "msg,expected",
    [
        # 既存ルートが新正規表現追加後も不変であること（衝突回帰）
        ("提案チェックして", "proposal_review"),
        ("飲食店のPR事例を教えて", "search"),
        ("森ビルのカルテ", "clientkarte"),
        ("このスレッドをログ化して", "operation_log"),
        ("提案作って", "proposal_draft"),
        # メール語単独・社内語なしは横断機能を発火させず search へ（誤爆防止・仕様）
        ("メール管理", "search"),
        ("メールの内容を確認", "search"),
    ],
)
def test_mail_features_do_not_break_existing_routes(msg: str, expected: str) -> None:
    assert detect_skill(msg).skill == expected


@pytest.mark.parametrize(
    "msg",
    ["返信してないんだよね", "なんか最近返信できてない", "もう返信できてないや"],
)
def test_venting_not_routed_to_mail_followup(msg: str) -> None:
    """『返信してない』だけの愚痴はメール語が近接しないので mail_followup を奪わない。"""
    assert detect_skill(msg).skill != "mail_followup"


@pytest.mark.parametrize(
    "msg,client,days",
    [
        ("花王への返信漏れ", "花王", None),
        ("A社の返信忘れ", "A社", None),
        ("森ビルの返信待ち", "森ビル", None),
        ("花王の返信してないメール", "花王", None),
    ],
)
def test_named_followup_extracts_client(msg: str, client: str, days: int | None) -> None:
    """指名つき要返信フレーズは client を抽出し、不要な再質問を避ける。"""
    it = detect_skill(msg)
    assert it.skill == "mail_followup"
    assert it.client_name == client
    assert it.followup_days == days


@pytest.mark.parametrize(
    "msg,skill",
    [
        ("過去提案のメール社内で見て", "mail_to_internal_context"),
        ("今日のメール、社内の関連スレッド", "mail_to_internal_context"),
        ("未読メール溜まってる", "mail_followup"),
    ],
)
def test_structural_words_not_used_as_client(msg: str, skill: str) -> None:
    """構造語(過去提案/今日/未読 等)は client にしない → None で再質問へ（誤った空検索を防ぐ）。"""
    it = detect_skill(msg)
    assert it.skill == skill
    assert it.client_name is None


@pytest.mark.parametrize(
    "msg,client",
    [
        ("森ビルへの返信作って", "森ビル"),
        ("マンダムのメール作成して", "マンダム"),
        ("花王のメールに返信ドラフト作って", "花王"),
        ("INPEXへの返信案ちょうだい", "INPEX"),
    ],
)
def test_routes_to_mail_reply(msg: str, client: str) -> None:
    """返信ドラフト/メール作成は mail_reply（_DRAFT_RE の『ドラフト』に奪われない）。"""
    it = detect_skill(msg)
    assert it.skill == "mail_reply"
    assert it.client_name == client


@pytest.mark.parametrize(
    "msg,client",
    [
        ("森ビルのメール要約して", "森ビル"),
        ("花王のメールまとめて", "花王"),
        ("マンダムのメールのサマリーちょうだい", "マンダム"),
    ],
)
def test_routes_to_mail_summary(msg: str, client: str) -> None:
    it = detect_skill(msg)
    assert it.skill == "mail_summary"
    assert it.client_name == client


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("提案ドラフト作って", "proposal_draft"),  # 提案ドラフトは mail_reply に奪われない
        ("提案チェックして", "proposal_review"),
        ("森ビルの要返信メール教えて", "mail_followup"),  # 要返信は mail_reply ではない
        ("森ビルのカルテ", "clientkarte"),
    ],
)
def test_mail_reply_summary_do_not_break_existing(msg: str, expected: str) -> None:
    assert detect_skill(msg).skill == expected


@pytest.mark.parametrize(
    "msg,client",
    [
        ("A社の提案の返信ドラフト作って", "A社"),  # 「○○社」を最優先抽出（提案に奪われない）
        ("花王さんのメール、返信案ちょうだい", "花王"),  # さん honorific + 読点
        ("森ビル様のメールに返信作って", "森ビル"),
    ],
)
def test_reply_client_extraction_robust(msg: str, client: str) -> None:
    it = detect_skill(msg)
    assert it.skill == "mail_reply"
    assert it.client_name == client


@pytest.mark.parametrize("msg", ["リプライ作成して", "返信用のメール作成して", "提案の返信作って"])
def test_reply_trigger_words_not_used_as_client(msg: str) -> None:
    """トリガー語/構造語（リプライ/返信用/提案）は client にしない → None で再質問。"""
    it = detect_skill(msg)
    assert it.skill == "mail_reply"
    assert it.client_name is None


def test_private_skills_cover_all_mail_skills() -> None:
    """メール系スキルは全て『本人にだけ返す（ephemeral）』対象に含める（チャンネル漏えい防止）。"""
    from teamagent.runtime.slack_bot import _PRIVATE_SKILLS

    assert {
        "mail_reply",
        "mail_summary",
        "mail_to_internal_context",
        "mail_followup",
    } <= _PRIVATE_SKILLS


@pytest.mark.parametrize(
    "msg",
    [
        "連携して",
        "メール連携したい",
        "Google連携お願い",
        "接続して",
        "connect",
        "アカウント連携",
        "連携リンクちょうだい",
    ],
)
def test_routes_to_connect(msg: str) -> None:
    """スラッシュコマンド未登録でも @メンション/DM の『連携』系で connect 経路に乗る。"""
    assert detect_skill(msg).skill == "connect"


@pytest.mark.parametrize("msg", ["他社との連携事例を教えて", "連携の進め方を調べて"])
def test_connect_does_not_steal_search(msg: str) -> None:
    """『連携事例』等は検索意図。connect に奪わせない（意図動詞/プレフィックス必須）。"""
    assert detect_skill(msg).skill != "connect"


def test_connect_is_private_delivery() -> None:
    """連携リンクは本人専用 → ephemeral 配信対象に含める。"""
    from teamagent.runtime.slack_bot import _PRIVATE_SKILLS

    assert "connect" in _PRIVATE_SKILLS
