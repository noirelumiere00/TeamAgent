"""メッセージ内容から起動すべき Skill を自動判定する (DB/LLM 非依存・純ロジック)。

ユーザーがスラッシュコマンドを使わずに @メンションするだけで、
「○○社のカルテ」→ clientkarte、「○○の提案作って」→ proposal_draft、
それ以外 → search、と自動振り分けするためのヒューリスティック。

曖昧なら search にフォールバック (search は RAG で大抵の質問に答えられる安全側)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# proposal_review 起動トリガー: 「レビュー/添削/診断/チェック」。draft より先に判定。
# 「フィードバック」は営業 FB と紛らわしいので採らない。
_REVIEW_RE = re.compile(r"レビュー|添削|診断|ブラッシュアップ|提案.{0,4}チェック|チェックして")

# proposal_draft 起動トリガー: 「提案」+ 作成系動詞、または「ドラフト/たたき台/骨子」
_DRAFT_RE = re.compile(
    r"提案.{0,6}(作|つく|ドラフト|たたき台|骨子|考え|練|まとめ)"
    r"|(作|つく|考え).{0,4}提案"
    r"|ドラフト|たたき台|提案骨子|どう提案"
)

# operation_log 起動トリガー: 「ログ化/活動ログ/営業ログ/CRM/議事録/記録して」等。
# Slack スレッドの会話を CRM 営業ログに構造化する意図。karte より先に判定
# (「記録」「ログ」は karte の「履歴」と紛れないよう明確な動詞・名詞のみ採用)。
_OPLOG_RE = re.compile(
    r"ログ化|活動ログ|営業ログ|商談ログ|CRM|議事録|"
    r"(?:会話|やり取り|スレッド|商談).{0,4}(?:記録|ログ|まとめ)|記録して"
)

# clientkarte 起動トリガー: 「カルテ」または「(の)状況/近況/温度感/履歴/どうなってる」等
_KARTE_TRIGGER = re.compile(r"カルテ|近況|温度感|どうなって|どんな感じ|今どう|状況|経緯|履歴")

# clientkarte のクライアント名抽出: トリガー語の手前の固有名詞っぽい部分を取る
_KARTE_EXTRACT = re.compile(
    r"(.+?)(?:の|は|って|が|に関して|について)?\s*"
    r"(?:カルテ|近況|温度感|どうなって|どんな感じ|今どう|状況|経緯|履歴)"
)

# 末尾に残りやすい助詞・記号 (抽出後のトリミング用)
_TRAILING = re.compile(r"[\s　]*(?:の|は|って|が|を|に|へ|、|。|！|!|？|\?)+$")


# 動画 URL (YouTube/Shorts/TikTok/Instagram)。検出したら video_analysis へ。
# Slack は <https://...> のように山括弧で包むことがあるため、それも許容して抽出する。
_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube\.com/\S+|youtu\.be/\S+|m\.youtube\.com/\S+"
    r"|tiktok\.com/\S+|instagram\.com/(?:reel|p)/\S+)"
)


# 一度に受け付ける動画 URL の上限 (コスト/レイテンシ防御)
MAX_VIDEO_URLS = 20


# TikTok 検索トリガー: 「TikTok/ティックトック」+「検索/調べ/リサーチ/探し」
# 例: 「TikTokで新宿 ランチ 検索して」「ティックトックで日焼け止め調べて」
# クエリが TikTok 名と検索動詞の間に入る (= 距離が空く) ため、近接ではなく
# 「TikTok 名の存在」AND「検索動詞の存在」で判定する。
_TIKTOK_NAME = r"(?:tiktok|ティックトック|ティクトック|ティックトック)"
_TIKTOK_NAME_RE = re.compile(_TIKTOK_NAME, re.IGNORECASE)
_SEARCH_VERB_RE = re.compile(r"検索|調べ|リサーチ|サーチ|探し|探って")


def _is_tiktok_search(text: str) -> bool:
    """TikTok 名と検索動詞が両方あれば TikTok 検索意図とみなす。"""
    return bool(_TIKTOK_NAME_RE.search(text) and _SEARCH_VERB_RE.search(text))


# ハッシュタグ検索トリガー: 「#新宿 で調べて」「#日焼け止め 検索」
# (URL ではない素の #語)。TikTok 文脈と解釈する。
_HASHTAG_SEARCH_RE = re.compile(
    r"[#＃]\s*([^\s#＃、。,]{1,40})\s*(?:で|を)?\s*"
    r"(?:調べ|検索|リサーチ|探し|見て|サーチ)"
)
# クエリ抽出用: 先頭の「TikTokで」等と、末尾の「(で/を) 検索して」等を削ぐ
_TIKTOK_NAME_PREFIX = re.compile(rf"^.*?{_TIKTOK_NAME}\s*(?:で|にて|から|の|を)?\s*", re.IGNORECASE)
_SEARCH_VERB_SUFFIX = re.compile(
    r"\s*(?:で|を|について|に関して)?\s*"
    r"(?:検索|調べ|リサーチ|サーチ|探し|探って)(?:して|て|てみて|てみたい|る)?"
    r"[\s　]*[。.!！?？]*\s*$"
)


# video_approval (動画一次FB審査) 起動トリガー: 「動画/納品 + チェック/審査/FB」または
# 「一次FB」「オリエン照合」「テロップチェック」。proposal_review (「チェックして」) より
# 先に、かつ「動画/納品」を伴う場合のみ採るので誤爆しにくい。
_VIDEO_APPROVAL_RE = re.compile(
    r"動画.{0,4}(?:チェック|審査|フィードバック|ＦＢ|FB|確認|レビュー)"
    r"|納品.{0,4}(?:チェック|確認|ＦＢ|FB|審査)"
    r"|一次\s*(?:ＦＢ|FB)|オリエン.{0,3}照合|テロップ.{0,3}チェック"
)
# 管理番号 (例: E01-01 / 1-1 / E08-02-c)。日本語助詞 (の/を) が直後に来ても拾えるよう、
# 境界は「数字の連続」だけを弾く (前後が数字＝より長い番号の一部、なら採らない)。
_MANAGEMENT_NO_RE = re.compile(r"(?<!\d)([A-Za-z]{0,3}\d{1,3}-\d{1,3}(?:-[A-Za-z0-9]{1,3})?)(?!\d)")
# メッセージ中の Google スプレッドシート URL から sheet_id を拾う (任意指定)。
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")

# video_algorithm (VSEO 動画アルゴリズム読み解き) 起動トリガー:
# 「VSEO分析」「アルゴリズム分析」「検索上位(を)分析」等。tiktok_search(=検索/調べ)とは
# 別物（"分析" は検索動詞に含めない）なので衝突しない。
_VSEO_ALGO_RE = re.compile(r"VSEO\s*分析|アルゴリズム\s*分析|検索\s*上位.{0,4}分析", re.IGNORECASE)
# KW 抽出用: トリガー語・プラットフォーム名・末尾の依頼語を削ぐ
_VSEO_STRIP = re.compile(
    r"VSEO|ヴイエスイーオー|アルゴリズム|検索\s*上位\s*\d*\s*本?|"
    r"tiktok|ティックトック|ティクトック|動画|分析|して(ほしい|ください)?|お願い",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillIntent:
    """自動ルーティングの判定結果。"""

    # search|clientkarte|proposal_draft|proposal_review|video_analysis|tiktok_search|
    # operation_log|video_approval
    skill: str
    client_name: str | None  # clientkarte のときのみ抽出
    reason: str
    video_url: str | None = None  # video_analysis 単一のときの先頭 URL (後方互換)
    video_urls: tuple[str, ...] = ()  # video_analysis の全 URL (複数一括対応)
    query: str | None = None  # tiktok_search の検索語
    search_type: str = "keyword"  # tiktok_search: keyword | hashtag
    management_no: str | None = None  # video_approval: 審査するクリエイティブの管理番号
    sheet_id: str | None = None  # video_approval: メッセージで明示されたシート (任意)


def _clean_url(raw: str) -> str:
    """Slack の <url> / <url|label> 括りを剥がす。"""
    return raw.split("|", 1)[0].rstrip(">")


def extract_video_urls(message: str, *, limit: int = MAX_VIDEO_URLS) -> list[str]:
    """メッセージから動画 URL を全て抽出する (重複除去・登場順・上限 limit)。"""
    seen: list[str] = []
    for m in _VIDEO_URL_RE.findall(message):
        # findall はグループ無しパターンなので全文マッチが返る
        url = _clean_url(m if isinstance(m, str) else m[0])
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def extract_video_url(message: str) -> str | None:
    """メッセージから動画 URL を 1 つ (先頭) 抽出する。Slack の <...> 括りも剥がす。"""
    urls = extract_video_urls(message, limit=1)
    return urls[0] if urls else None


def _extract_client_name(message: str) -> str | None:
    m = _KARTE_EXTRACT.match(message.strip())
    if not m:
        return None
    name = _TRAILING.sub("", m.group(1).strip())
    # 1 文字や空は固有名詞として弱いので採用しない
    return name if len(name) >= 2 else None


# TikTok 検索クエリの前後から削ぎ落とす語 (命令文の定型)
_TIKTOK_STRIP = re.compile(r"^(?:で|にて|から|の|を|について)\s*|\s*(?:について|に関して)$")


def _extract_tiktok_query(message: str) -> tuple[str | None, str]:
    """TikTok 検索の (query, search_type) を抽出する。

    - 「#新宿 で調べて」→ ("新宿", "hashtag")
    - 「TikTokで新宿 ランチ で検索して」→ ("新宿 ランチ", "keyword")
    抽出できなければ (None, "keyword")。
    """
    text = message.strip()

    # ハッシュタグ優先 (# が付いていれば hashtag 意図)
    mh = _HASHTAG_SEARCH_RE.search(text)
    if mh:
        q = mh.group(1).strip()
        if len(q) >= 1:
            return q, "hashtag"

    # 「TikTokで <query> 検索して」: 先頭の TikTok 名句と末尾の検索動詞句を削ぐ
    q = _TIKTOK_NAME_PREFIX.sub("", text, count=1)
    q = _SEARCH_VERB_SUFFIX.sub("", q, count=1)
    q = _TIKTOK_STRIP.sub("", q.strip()).strip()
    q = _TRAILING.sub("", q).strip()
    if len(q) >= 1:
        return q, "keyword"

    return None, "keyword"


def _extract_vseo_query(message: str) -> str | None:
    """「VSEO分析 新宿ランチ」「○○をアルゴリズム分析して」→ 検索KW を抜く。"""
    q = _VSEO_STRIP.sub(" ", message)
    q = re.sub(r"[\s　]+", " ", q).strip()
    q = _TRAILING.sub("", q).strip()
    q = re.sub(r"^[\sでをのにへ、,]+|[\sでをのにへ、,]+$", "", q).strip()
    return q or None


# --- 会話/雑談ルーティング（task-first: 業務語があれば必ず検索/各Skillへ流す） ---
# 業務・検索を示唆する語。1つでも含めば chitchat にしない（複合文「ありがとう、A社の提案は?」を
# search に残す＝実検索の取りこぼし防止）。社名マーカー(社/御社/弊社)も task 扱い。
_TASK_HINT_RE = re.compile(
    r"提案|案件|顧客|クライアント|カルテ|資料|事例|実績|見積|予算|BANT|商談|施策|競合|"
    r"分析|動画|tiktok|ティック|検索|調べ|まとめ|レビュー|添削|診断|ログ|オリエン|"
    r"株式会社|御社|弊社|[ぁ-んァ-ヶ一-龯A-Za-z0-9]社(?:さん|様)?",
    re.IGNORECASE,
)
# 能力・使い方の質問（Bot は何ができる？）。task ガードより優先（「機能教えて」を拾う）。
_CAPABILITY_RE = re.compile(
    r"何が?でき|なにが?でき|使い方|どう使|ヘルプ|help|"
    r"機能.{0,4}(教え|一覧|ある|は|？|\?)|コマンド.{0,4}(教え|一覧|ある)|できること",
    re.IGNORECASE,
)
# 挨拶（文頭一致）
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|yo|やあ|やほ|ヤッホ|おは|こんにち[はわ]|こんばん[はわ]|"
    r"はじめまして|久しぶり|おひさ|お疲れ|おつかれ|おつ[!！]?$|どうも)",
    re.IGNORECASE,
)
# お礼・相槌（ほぼ全体一致）
_THANKS_BACKCHANNEL_RE = re.compile(
    r"^(?:"
    r"ありがとう?(?:ございま(?:す|した))?|あざ(?:っ?す|ます)|"
    r"thanks?|thank\s*you|サンキュ[ーう]?|感謝(?:します)?|"
    r"助か(?:る|った|ります|りました)|"
    r"ok|okay|おけ|おk|了解(?:です)?|りょう?(?:かい)?|"
    r"わか(?:った|りました)|なるほど|いいね|そうだね|"
    r"はい+|うん+|大丈夫(?:です)?|👍|🙏|😊|🙆|👏"
    r")[\s。、.!！？?〜ー]*$",
    re.IGNORECASE,
)
# 記号・絵文字のみ（「？」「👍」「!!」等）
_SYMBOLS_ONLY_RE = re.compile(r"^[\W_\s]+$", re.UNICODE)


def _is_chitchat(text: str) -> bool:
    """雑談/挨拶/お礼/相槌/能力質問なら True。task-first: 業務語を含めば False（検索へ）。"""
    if _CAPABILITY_RE.search(text):  # 「何ができる?」等は最優先
        return True
    if _TASK_HINT_RE.search(text):  # 業務語/社名 → 雑談ではない（必ず検索/各Skillへ）
        return False
    return bool(
        _GREETING_RE.match(text)
        or _THANKS_BACKCHANNEL_RE.match(text)
        or _SYMBOLS_ONLY_RE.match(text)
    )


def detect_skill(message: str) -> SkillIntent:
    """メッセージから起動 Skill を判定する。

    優先順位: 動画/VSEO/TikTok/審査/提案/oplog/カルテ → 雑談(chitchat) → search (既定)。
    """
    text = message.strip()

    # 0. 動画 URL があれば最優先で動画分析 (URL は強い意図シグナル)。複数対応。
    video_urls = extract_video_urls(text)
    if video_urls:
        return SkillIntent(
            skill="video_analysis",
            client_name=None,
            reason=f"video url detected ({len(video_urls)})",
            video_url=video_urls[0],
            video_urls=tuple(video_urls),
        )

    # 0a-2. VSEO 動画アルゴリズム読み解き (「VSEO分析 ○○」「○○をアルゴリズム分析」)。
    # tiktok_search より先に判定（"分析" は検索動詞でないので元々衝突しないが明示的に優先）。
    if _VSEO_ALGO_RE.search(text):
        q = _extract_vseo_query(text)
        return SkillIntent(
            skill="video_algorithm",
            client_name=None,
            reason="vseo algorithm trigger",
            query=q,
        )

    # 0b. TikTok 検索意図 (「TikTokで○○検索して」「#○○ で調べて」)。
    # 社内 RAG 検索 (search) と紛れないよう、TikTok 名 or ハッシュタグが明示された
    # ときのみ tiktok_search に倒す。URL の次・提案系より前に判定。
    if _is_tiktok_search(text) or _HASHTAG_SEARCH_RE.search(text):
        q, stype = _extract_tiktok_query(text)
        if q:
            return SkillIntent(
                skill="tiktok_search",
                client_name=None,
                reason=f"tiktok search trigger ({stype})",
                query=q,
                search_type=stype,
            )

    # 0c. 動画一次FB審査 (オリエン照合)。「動画/納品 + チェック」+ 管理番号で判定。
    # proposal_review の「チェックして」より先に、かつ「動画/納品/一次FB」を伴うときのみ。
    if _VIDEO_APPROVAL_RE.search(text):
        mno = _MANAGEMENT_NO_RE.search(text)
        sid = _SHEET_ID_RE.search(text)
        return SkillIntent(
            skill="video_approval",
            client_name=None,
            reason="video approval trigger",
            management_no=mno.group(1) if mno else None,
            sheet_id=sid.group(1) if sid else None,
        )

    # 1a. 提案レビュー意図 (レビュー/添削/診断)。draft より先に判定。
    if _REVIEW_RE.search(text):
        return SkillIntent(skill="proposal_review", client_name=None, reason="review trigger")

    # 1b. 提案ドラフト作成意図 (動詞が明確)
    if _DRAFT_RE.search(text):
        return SkillIntent(skill="proposal_draft", client_name=None, reason="draft trigger")

    # 1c. 営業活動ログ (会話→CRM ログ)。カルテより先に判定。
    if _OPLOG_RE.search(text):
        return SkillIntent(skill="operation_log", client_name=None, reason="oplog trigger")

    # 2. クライアントカルテ (トリガー + クライアント名が抽出できたときのみ)
    if _KARTE_TRIGGER.search(text):
        client = _extract_client_name(text)
        if client:
            return SkillIntent(
                skill="clientkarte", client_name=client, reason="karte trigger + client"
            )

    # 2b. 雑談/挨拶/お礼/能力質問 → 会話応答（task-first: 上のトリガ全不一致 ∧ 業務語なしのみ）
    if _is_chitchat(text):
        return SkillIntent(skill="chitchat", client_name=None, reason="chitchat/greeting")

    # 3. 既定は横断検索
    return SkillIntent(skill="search", client_name=None, reason="default search")
