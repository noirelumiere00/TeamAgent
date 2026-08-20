"""client_name（お客様名）の意味検査ガード（純粋関数のみ・IO/DB 非依存）。

## なぜ要るか（実測された事故）

外側ルーターは自然文から引数を作るため、依頼文の断片をそのまま client_name に詰める:

    「今週の空いてる時間を教えて」→ mail_followup(client_name="今週の空き時間") → scanned=0
    「返信が必要なメールを教えて」→ mail_summary(client_name="返信必要")     → scanned=0
    「今日届いたメールを要約して」→ mail_summary(client_name="今日のメール")   → scanned=0

skill 側は ``'"{client_name}" newer_than:14d'`` という **Gmail の完全一致フレーズ検索**を
組むので、上記は必ず 0 件になる。利用者からは「メール連携が壊れている」ようにしか見えないが、
実際には連携は正常で、単に存在しないフレーズを検索していただけだった。

## 判定方式: 残差法（単純部分一致は禁止）

「メール」等の構造語を **部分一致で含むから弾く**、をやると「日本メール便」のような正当な
固有名詞まで殺す。そこで:

1. 正規化（NFKC・空白畳み込み・``"`` と ``\\`` の除去・括弧の除去）
2. 正規化文字列から **構造語・助詞・記号** を削る
3. 残った「固有名詞残差」の長さで判定する

    * 残差 0〜1 文字 → ``structural``（依頼文の断片であり、お客様名ではない）
    * 正規化後が空   → ``missing``
    * 残差 == 正規化原文 → ``ok`` / search_terms=[原文]
    * 残差 < 原文 かつ 2 文字以上 かつ **非ひらがな 2 文字以上** →
      ``ok`` / search_terms=[原文, 残差]（**二段検索用**）
    * 残差 < 原文 だが上の条件を満たさない → ``ok`` / search_terms=[原文]（2 本目を出さない）

二段検索: 1 本目（原文フレーズ）が 0 件のときだけ 2 本目（残差フレーズ）を引く。
「花王のメール」は 1 本目 ``"花王のメール"`` が 0 件でも 2 本目 ``"花王"`` で救える。

⚠️ **2 本目の非ひらがな要件**（2026-08-20 レビュー 要修正1 の実測）: 残差が動詞・助動詞の
活用（「している」「してる」「届いた」「たまってる」）だけになる入力が実在する。この残差で
2 本目を引くと **無関係な他社のメールがヒットし、それを元の client_name の要約として自信
満々に返す**（error="" / connection="live" のまま帰属を誤る＋Bedrock を 1 回課金）。
実測: ``client_name="放置しているメール"`` → 2 本目 ``"している"`` → 他社 2 件を要約。
残差に非ひらがなが 2 文字以上あることを要求すると、「花王」「日本便」「セブン」「30分MTG」
は通り、「している」「してる」「届いた」「たまってる」は落ちる。落ちても verdict は ``ok``
のままで、1 本目（原文フレーズ）の 0 件＝**正直な 0 件**に着地する。

副作用（既知・``tests/skills/test_client_name_guard_contract.py`` の
``HIRAGANA_NAMES_LOSING_THE_SECOND_STAGE``）: 全ひらがなの実在社名＋構造語
（「とらやのメール」）も 2 本目を失う。1 本目で 0 件なら「0 件でした」と正直に返るだけで、
別会社のメールを掴むことはない＝**安全側の失敗**なのでこちらを採る。

助詞の削除は「ひらがなが 3 文字以上連続するかたまり」には適用しない。「とらや」「はなまる
うどん」のような全ひらがな固有名詞が助詞削除で 1 文字まで削れて structural 誤判定になるのを
防ぐため（構造語を削った後の「の」「のの」のような短い残りだけを助詞として落とす）。

## 2 本目を引いたことは必ず開示する

残差は構造語を **内側から** 削るため断片が溶接されうる（「東京メール大学」→「東京大学」＝
実在の別法人／「日本メール便」→「日本便」）。2 本目の結果を元の client_name の名前で黙って
提示すると帰属を誤るので、:func:`retry_disclosure` / :func:`retry_zero_note` で
「どの語で引き直したか」を利用者向け文面に必ず出す（呼び出し側の責務）。

## セキュリティ

正規化後にコロンを含む入力（``x" OR from:ceo@example.com "`` 等）は Gmail 検索演算子の
インジェクション試行とみなし ``structural`` で拒否する。``to_gmail_phrase`` は ``"`` を
除去してからフレーズで括るため、フレーズを閉じてクエリを継ぎ足すことはできない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from teamagent.observability import scrub_value

ClientNameVerdictLabel = Literal["ok", "structural", "missing"]

# 正規化で落とす文字。``"`` と ``\`` は Gmail クエリのフレーズを壊すため常に除去し、
# 括弧は「(株)ABC」を壊さないために **拒否ではなく除去** する（NFKC で（）→() 済み）。
_DROP_CHARS: Final[frozenset[str]] = frozenset('"\\()（）')

_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

# 構造語（＝依頼文の骨格。お客様名ではない語）。skills/intent.py の
# _MAIL_CLIENT_STOPWORDS を種に、実測された事故値（今週の空き時間 / 返信必要 /
# 今日のメール）を残差 0 にできるところまで拡張したもの。
# 注意: これは「含んだら弾く」リストではない。**残差法で削る対象**であり、
# 「日本メール便」は「メール」を削っても残差「日本便」が残るので ok と判定される。
_STRUCTURAL_TOKENS: Final[tuple[str, ...]] = (
    # 受信箱・メールそのもの
    "受信箱",
    "受信",
    "メールボックス",
    "メール",
    "mail",
    "inbox",
    "スレッド",
    "スレ",
    # 返信・トリアージ状態
    "要返信",
    "未返信",
    "返信必要",
    "返信漏れ",
    "返信待ち",
    "返信忘れ",
    "返信用",
    "返信",
    "リプライ",
    "トリアージ",
    "未読",
    "放置",
    "必要",
    # 期間・時制
    "今日",
    "昨日",
    "明日",
    "今週",
    "来週",
    "先週",
    "今月",
    "先月",
    "直近",
    "最近",
    # 予定・空き時間（mail_* に誤ルーティングされた calendar 系依頼の断片）
    "スケジュール",
    "空いてる",
    "空いて",
    "空き",
    "時間",
    "予定",
    # 依頼動詞・数量詞
    "一覧",
    "まとめ",
    "要約",
    "全部",
    "すべて",
    "教えて",
    "ある",
    "件",
    # 文脈語（intent.py 由来）
    "関連",
    "過去提案",
    "提案書",
    "提案",
    "社内",
    "文脈",
    "近況",
    "履歴",
    "状況",
    "経緯",
    # 指示語
    "この",
    "その",
    "あの",
    # 代名詞・法人格だけの語（具体的なお客様名ではない）
    "御社",
    "貴社",
    "弊社",
    "当社",
    "自社",
    "株式会社",
    "有限会社",
    "合同会社",
)

# 長い語から先に削る（「返信必要」を「返信」＋「必要」に割らない）。
_STRUCTURAL_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(t) for t in sorted(_STRUCTURAL_TOKENS, key=len, reverse=True)),
    re.IGNORECASE,
)

# 助詞（構造語を削った後に残る接着剤）。
_PARTICLES: Final[tuple[str, ...]] = (
    "から",
    "まで",
    "の",
    "が",
    "を",
    "に",
    "は",
    "へ",
    "と",
    "で",
    "も",
    "や",
)
_PARTICLE_CHARS: Final[frozenset[str]] = frozenset("".join(_PARTICLES))

# ひらがな連続がこの長さを超えたら固有名詞とみなして助詞削除を適用しない。
_MAX_PARTICLE_RUN: Final[int] = 2
_HIRAGANA_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[ぁ-ゖ]+")

# 記号（残差計算でのみ落とす）。``ー`` ``・`` ``&`` は名前を構成するので残す。
_SYMBOL_CHARS: Final[frozenset[str]] = frozenset(
    " \t\n\r!\"#$%'()*+,-./:;<=>?@[\\]^_`{|}~、。，．…〜～「」『』【】〔〕〈〉《》"
)

# 残差がこの長さ以下なら「お客様名の実体が無い」＝ structural。
_MIN_RESIDUAL_LEN: Final[int] = 2

# 2 本目（残差フレーズ）を発行してよい最小の非ひらがな文字数。
# 「している」「してる」「届いた」「たまってる」のような活用の残りかすで受信箱を引くと、
# 無関係なメールを client_name の要約として返してしまう（module docstring の実測）。
_MIN_RESIDUAL_NON_HIRAGANA: Final[int] = 2

# 「非ひらがな」を数えるときに **名前の実体として数えない** 文字（伸ばし棒・中黒・空白）。
_NON_NAME_CHARS: Final[frozenset[str]] = frozenset("ー・ｰ 　\t")
_HIRAGANA_RE: Final[re.Pattern[str]] = re.compile(r"[ぁ-ゖ]")

# エコーは scrub_value でマスクした上でこの長さに切る。
_ECHO_MAX_CHARS: Final[int] = 30

# Output.error に載せる決定論コード（呼び出し側が分岐に使える）。
ERROR_BY_VERDICT: Final[dict[str, str]] = {
    "structural": "client_name_structural",
    "missing": "client_name_missing",
}

MSG_MISSING: Final[str] = (
    "どちらのお客様のメールを見ればよいですか？"
    "（連携は正常です。お客様名を教えていただければすぐ確認します。"
    "例:「花王」「アサヒ飲料」）"
)


@dataclass(frozen=True)
class ClientNameVerdict:
    """client_name の意味検査結果（純粋な値）。

    Attributes:
        verdict: ``ok`` / ``structural``（依頼文の断片）/ ``missing``（空）。
        normalized: 正規化後の文字列（エコー表示・検索キーワードの原文）。
        search_terms: 検索に使うキーワード。1〜2 個。1 個目＝原文、2 個目＝残差。
        reason: 判定理由（ログ用の決定論コード。値そのものは含まない）。
    """

    verdict: ClientNameVerdictLabel
    normalized: str
    search_terms: list[str]
    reason: str


def normalize_client_name(raw: str | None) -> str:
    """NFKC 正規化 → 記号除去 → 空白畳み込み → strip。

    ``"`` と ``\\`` は Gmail クエリを壊すので除去。括弧は「(株)ABC」を壊さないよう
    **拒否ではなく除去**する（→「株ABC」）。全角空白・連続空白は 1 個の半角空白に畳む。
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = "".join(ch for ch in s if ch not in _DROP_CHARS)
    return _WS_RE.sub(" ", s).strip()


def _strip_particles(text: str) -> str:
    """助詞を落とす。ただしひらがな 3 文字以上のかたまりは固有名詞として温存する。"""

    def _drop(m: re.Match[str]) -> str:
        run = m.group(0)
        if len(run) > _MAX_PARTICLE_RUN:
            return run
        return "".join(ch for ch in run if ch not in _PARTICLE_CHARS)

    return _HIRAGANA_RUN_RE.sub(_drop, text)


def residual_of(normalized: str) -> str:
    """正規化文字列から構造語・助詞・記号を削った「固有名詞残差」を返す。"""
    stripped = _STRUCTURAL_RE.sub("", normalized)
    stripped = _strip_particles(stripped)
    return "".join(ch for ch in stripped if ch not in _SYMBOL_CHARS)


def non_hiragana_len(text: str) -> int:
    """残差のうち「名前の実体になりうる非ひらがな文字」の数。

    漢字・カタカナ・英数字を数え、ひらがな・伸ばし棒・中黒・空白は数えない。
    「している」=0 / 「届いた」=1 / 「花王」=2 / 「セブン」=3 / 「30分MTG」=6。
    """
    return sum(
        1 for ch in str(text or "") if ch not in _NON_NAME_CHARS and not _HIRAGANA_RE.match(ch)
    )


def is_searchable_residual(residual: str, normalized: str) -> bool:
    """残差を **2 本目の検索フレーズとして発行してよいか**（module docstring の実測根拠）。

    活用の残りかす（「している」「してる」「届いた」）で受信箱を引くと、無関係なメールを
    元の client_name の要約として返す誤答になる。安全側＝「引かない」に倒す。
    """
    if not residual or residual == normalized:
        return False
    if len(residual) < _MIN_RESIDUAL_LEN:
        return False
    return non_hiragana_len(residual) >= _MIN_RESIDUAL_NON_HIRAGANA


def classify_client_name(raw: str | None) -> ClientNameVerdict:
    """client_name が「お客様名」として使えるかを残差法で判定する。

    Gmail は一度も叩かない純粋関数。呼び出し側は ``verdict != "ok"`` なら
    受信箱を検索せずに案内文（:func:`guard_message`）を返すこと。
    """
    normalized = normalize_client_name(raw)
    if not normalized:
        return ClientNameVerdict(verdict="missing", normalized="", search_terms=[], reason="empty")
    # Gmail 検索演算子（from: / subject: / label: …）の持ち込みは拒否する。
    if ":" in normalized or "：" in normalized:
        return ClientNameVerdict(
            verdict="structural", normalized=normalized, search_terms=[], reason="operator_colon"
        )
    residual = residual_of(normalized)
    if len(residual) < _MIN_RESIDUAL_LEN:
        return ClientNameVerdict(
            verdict="structural",
            normalized=normalized,
            search_terms=[],
            reason="structural_only",
        )
    if residual == normalized:
        return ClientNameVerdict(
            verdict="ok", normalized=normalized, search_terms=[normalized], reason="proper_noun"
        )
    if not is_searchable_residual(residual, normalized):
        # 残差が活用の残りかす（「している」等）。**2 本目は出さない**が、原文フレーズでの
        # 検索は通す（1 本目が 0 件なら「正直な 0 件」に着地する＝誤帰属しない）。
        return ClientNameVerdict(
            verdict="ok",
            normalized=normalized,
            search_terms=[normalized],
            reason="proper_noun_weak_residual",
        )
    return ClientNameVerdict(
        verdict="ok",
        normalized=normalized,
        search_terms=[normalized, residual],
        reason="proper_noun_with_residual",
    )


def to_gmail_phrase(term: str) -> str:
    """Gmail の完全一致フレーズ（``"..."``）を組む。``"`` は除去してから括る。"""
    return '"' + term.replace('"', "") + '"'


def _echo(text: str) -> str:
    """エコー用に PII/シークレットをマスクし、30 字で切る。"""
    return str(scrub_value(str(text or "")))[:_ECHO_MAX_CHARS]


def safe_client_name(raw: str | None) -> str:
    """``Output.client_name`` や LLM プロンプトに載せてよい形へ落とす（正規化→マスク→短縮）。

    2026-08-20 レビュー 要修正1（HIGH）: 同じ応答の中で ``message`` は
    ``[REDACTED_PII]`` なのに ``client_name`` は生値、という**二重基準**が実測された。
    ``client_name`` は MCP の ``model_dump()`` で外側 LLM 文脈へ入り、Slack のヘッダ
    （``*📨 {client_name} — メール要約*``）にもそのまま出るので、エコーする以上は
    :func:`msg_structural` と同じ規律（scrub＋短縮）を通す。
    """
    return _echo(normalize_client_name(raw))


def retry_disclosure(original: str, used: str) -> str:
    """二段検索の 2 本目で当てたことを開示する一文（帰属の誤認防止）。

    残差は構造語を内側から削るため「東京メール大学」→「東京大学」のように**別法人**へ
    化けうる。どの語で当たったかを出さずに元の client_name の名前で要約を出すと、
    利用者は別クライアントのメールを自分の案件だと読んでしまう。
    """
    return (
        f"※「{_echo(original)}」では 0 件だったため「{_echo(used)}」で検索し直した結果です"
        "（別のお客様のメールが混ざっていたら、正式名称で言い直してください）。"
    )


def retry_zero_note(used: str) -> str:
    """2 本目も 0 件だったことを開示する一文（何を試したかを黙らない）。"""
    return f"（「{_echo(used)}」でも検索し直しましたが 0 件でした）"


def msg_structural(echo: str) -> str:
    """依頼文の断片が来たときの決定論案内文（連携は正常だと明示する）。"""
    return (
        f"『{_echo(echo)}』はお客様名として扱えませんでした"
        "（連携は正常です。まだ受信箱は検索していません）。"
        "どちらのお客様のメールでしょうか？ 例:「花王の最近のやりとり」"
    )


def guard_message(verdict: ClientNameVerdict) -> str:
    """verdict に対応する利用者向け案内文。``ok`` なら空文字。

    エコーには **正規化後の文字列**を使う（``"`` や ``\\`` を落としてあるぶん安全で、
    利用者にとっては自分が言った語として十分に識別できる）。
    """
    if verdict.verdict == "missing":
        return MSG_MISSING
    if verdict.verdict == "structural":
        return msg_structural(verdict.normalized)
    return ""


__all__ = [
    "ERROR_BY_VERDICT",
    "MSG_MISSING",
    "ClientNameVerdict",
    "ClientNameVerdictLabel",
    "classify_client_name",
    "guard_message",
    "is_searchable_residual",
    "msg_structural",
    "non_hiragana_len",
    "normalize_client_name",
    "residual_of",
    "retry_disclosure",
    "retry_zero_note",
    "safe_client_name",
    "to_gmail_phrase",
]
