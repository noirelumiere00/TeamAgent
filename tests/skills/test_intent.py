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
