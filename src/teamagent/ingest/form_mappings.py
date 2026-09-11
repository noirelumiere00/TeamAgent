"""ナレッジ共有フォーム回答シート (gsheets) の列 → 構造化メタ写像。

2026-07-06 追加。営業 FB シート (slack_fb_parser.map_fb_fields, 549dca9) と同じ流儀で、
「ナレッジ共有 - フォーム回答」シート (sheet_id 1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo /
gid 278789217・row_unit) の行に first-class メタを付ける。実ヘッダ (Drive API で取得した
実データ 190 行・2026-07-03 dump) は:

    ファイルをアップ / 正式社名 / 案件名 / クライアント種別 / 提案プロダクト /
    資料の概要 / このナレッジのポイントはここ！ / なぜそのナレッジ（資料）を共有したのか？ /
    フリーコメント / 送信者 / タイムスタンプ / ドライブ格納 / 保管先フォルダID記録（GAS処理)

写像先の決定は **実データの値域を確認して** 行った (推測でない):

- 正式社名 → client_company (生値) ＋ client_name (清浄化・first-class)。
  実値は「株式会社GA technologies」「カゴメ様」「ロート製薬（代理店：博報堂）」
  「集英社／キリンビバレッジ／ドン・キホーテ」等の揺れを含む。search の _match_client は
  「既知 client_name ⊂ クエリ」の substring 一致なので、法人格 (株式会社等)・敬称 (様/さま)・
  末尾括弧注記・複数社連記を落とさないとブーストが発火しない。FB 経路の client_name は
  法人格なし表記 (例 'SCSK') なので、それと同品質へ derive_knowledge_client_name で寄せる。
- 案件名 → client_case (FB と同キー・「案件を識別する人間入力」軸を 1 本に保つ。
  karte timeline は is_sales_fb='true' で絞るためナレッジ行が誤流入することはない)。
- クライアント種別 → client_type (独自キー)。実値域は企業属性
  (その他 65 / TOP500 or ベス10 59 / 上場企業 21 / メーカー 20 / 官公庁、自治体 4 ＋
  カンマ多選択) であり、cls_industry / industry の業種語彙 (食品/化粧品/IT…) とは別軸。
  industry キーは search の filter_industry (soft-strict) に直結するため、
  「TOP500 or ベス10」を流し込むと業界フィルタが壊れる → cls_* に寄せず独立キー。
- 提案プロダクト → proposed_menu (FB の「提案メニュー」と同キー・人間入力)。実値域は
  自社プロダクト名 (その他 87 / ショート動画提案 / ビデオリリース / ソリューションプラン /
  タテガタ / NCS / SWIPE VIDEO KIT…・カンマ多選択 42 パターン) で、cls_solution の正規語彙
  _SOLUTIONS (SNS運用/動画広告/…) には寄らない。cls_solution は Haiku の名前空間
  (合成順 fb/knowledge → cls で cls が後勝ち) のため人間入力を混ぜず、FB と同じ
  proposed_menu に載せて ILIKE 横断集計を可能にする。
- 資料の概要 → knowledge_kind (独自キー)。実値域 (提案 121 / レポート 17 /
  その他ナレッジ / 社内共有情報 / クロージング / AI活用…・多選択) は cls_doc_type の語彙
  (提案書/議事録/報告書/価格表/契約/その他) と粒度が合わず 1:1 に潰せない →
  cls_doc_type は Haiku のまま残し、人間入力は独立キーで併存させる。
- このナレッジのポイントはここ！ → knowledge_point / なぜ…共有したのか？ → share_reason
  (実データでは全行空だが列は実在する。空値は drop されるので害ゼロ・運用開始に備えた写像)。
- 送信者 → submitter (「誰がこの知見を持つか」の人物軸。実値は表示名/メール混在の生値のまま)。
- 写像しない列: ファイルをアップ (Slack file URL・運用列)・フリーコメント (長文自由記述で
  行本文=embedding 対象に全文が既に入る。metadata は構造化フィルタの名前空間なので入れない)・
  ドライブ格納 / 保管先フォルダID記録 (GAS 運用列)。タイムスタンプは metadata JSONB には
  複製せず、pipeline が documents.modified_at の正本として別途利用する。

識別は FB (map_fb_fields) と同じ「このシート固有のコアヘッダ閾値」方式:
コアヘッダ (正式社名/案件名/クライアント種別/提案プロダクト/資料の概要) が 3 つ以上
存在するときだけ写像し、それ以外のシートには空 dict ＝副作用ゼロ。FB シートのヘッダ
(商流/顧客名/顧客名・案件名/…) はコアと 1 つも交差しないため相互誤爆しない
(「顧客名・案件名」は正規化後も「案件名」と一致しない)。

is_sales_fb は立てない (これは FB ではなくナレッジ共有)。代わりに pipeline 側が
is_knowledge_share=True を付ける。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import NamedTuple

# ヘッダ (canonical 形) → metadata JSONB key の写像。実ヘッダ 2026-07-03 確認。
# 新規列が追加されたらここに足すだけ。lookup 前に _normalize_form_label を通すこと。
_KNOWLEDGE_LABEL_TO_METADATA_KEY: dict[str, str] = {
    "正式社名": "client_company",
    "案件名": "client_case",
    "クライアント種別": "client_type",
    "提案プロダクト": "proposed_menu",
    "資料の概要": "knowledge_kind",
    # ファイル記録シート (gid 1962561294) は同義の別ヘッダ「資料の概要_メイン」を使う。
    # フォーム回答シートの「資料の概要」と同じ knowledge_kind へ寄せる。カテゴリの正本は
    # この knowledge_kind の 1 本 (export_vault が knowledge_kind AS cls_category で射影)。
    # 実 dump (2026-07-15・ファイル記録 342 行) の値域は **素の値**:
    #   提案254 / レポート46 / 社内共有情報28 / AI活用（プロンプト等）8 / クロージング4 /
    #   オリエン資料1 / 競合資料1。GAS が保管先フォルダ(01_提案 等)へ振り分ける際の種別そのもの。
    # 「保管先フォルダ」列(値は "01_提案" の NN_ 付き)とは **完全ミラー**（342 行で実測一致）。
    # NN_ 接頭の有無だけが差なので冗長として写像しない。99_一次倉庫 の除外は
    # pipeline._ingest_gsheet が gdrive と同一 regex で行ごと落とす（GAS は本番フォルダへ移動して
    # から記録するため保管先フォルダに 99_ は出ない＝実測 0 件・将来行への保険）。
    "資料の概要_メイン": "knowledge_kind",
    "このナレッジのポイントはここ！": "knowledge_point",
    "なぜそのナレッジ（資料）を共有したのか？": "share_reason",
    "送信者": "submitter",
}

# ナレッジ共有シートらしさを判定する最小条件 (FB の _FB_CORE_LABELS/_FB_MIN_CORE_HITS と
# 同設計)。「案件名」単独は一般的すぎるためコア 5 つ中 3 つを要求し、無関係シートの
# 誤爆を防ぐ。実シートは 5 つ全部を持つ。
_KNOWLEDGE_CORE_LABELS = frozenset(
    {"正式社名", "案件名", "クライアント種別", "提案プロダクト", "資料の概要"}
)
_KNOWLEDGE_MIN_CORE_HITS = 3

# client_name として意味を成さないプレースホルダ (実データで観測: なし/その他/色々)。
_CLIENT_NAME_PLACEHOLDERS = frozenset(
    {"なし", "無し", "その他", "色々", "不明", "未定", "-", "ー", "―"}
)

# 末尾の括弧注記 (「ロート製薬（代理店：博報堂）」「ユニー（商業施設）」等)。全角/半角両対応。
_TRAILING_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]\s*$")

# 法人格の prefix/suffix (「株式会社GA technologies」「TOTO株式会社」の両形が実在)。
_CORPORATE_AFFIXES: tuple[str, ...] = ("株式会社", "（株）", "(株)", "有限会社", "合同会社")

# 敬称 suffix (「カゴメ様」「株式会社ネオジャパンさま」が実在)。
_HONORIFIC_SUFFIXES: tuple[str, ...] = ("様", "さま", "さん")


def _normalize_form_label(label: str) -> str:
    """シートヘッダの表記ゆれを canonical ラベルへ正規化する。

    slack_fb_parser._normalize_fb_label と同じ流儀 (前後 whitespace 除去・半角括弧 →
    全角括弧・全角スラッシュ → 半角) に加え、フォーム質問文の末尾記号ゆれ
    (半角 '!'/'?' → 全角) を吸収する。
    """
    return (
        label.strip()
        .replace("(", "（")
        .replace(")", "）")
        .replace("／", "/")
        .replace("!", "！")
        .replace("?", "？")
    )


def map_knowledge_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """ヘッダ → 値 の dict をナレッジ共有 metadata JSONB 用 dict に写像する。

    Returns:
        - 正規化後のコアヘッダが _KNOWLEDGE_MIN_CORE_HITS 個以上見つかった場合:
          metadata key → 非空値 の dict (空値の列は含めない)
        - コアヘッダ不足 (= ナレッジ共有フォームではない) の場合: 空 dict {}
          → 非対象シートへの副作用ゼロ (FB シート・任意のシートで {} をテストで固定)

    コアヘッダ判定は「ヘッダ (列) の存在」で行い値の有無は問わない (map_fb_fields と同一)。
    既知ヘッダ以外は無視する。
    """
    if not fields:
        return {}

    normalized: dict[str, str] = {}
    for label, value in fields.items():
        canonical = _normalize_form_label(label)
        # 表記ゆれで同一 canonical に潰れた場合は非空値を優先 (空値で上書きしない)
        if canonical not in normalized or value.strip():
            normalized[canonical] = value

    core_hits = len(normalized.keys() & _KNOWLEDGE_CORE_LABELS)
    if core_hits < _KNOWLEDGE_MIN_CORE_HITS:
        return {}

    out: dict[str, str] = {}
    for canonical, value in normalized.items():
        key = _KNOWLEDGE_LABEL_TO_METADATA_KEY.get(canonical)
        if key is None:
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        out[key] = cleaned

    return out


def derive_knowledge_client_name(company: str) -> str | None:
    """正式社名 (フォーム生値) から検索用の「主クライアント名」を導出する。

    FB 経路の extract_client_name と同品質 (法人格なしの bare entity) に寄せる。
    実データ 155 ユニーク値で確認した揺れへの対処 (順序が重要):

    1. プレースホルダ (なし/その他/色々 等) → None
    2. 複数社連記「集英社／キリンビバレッチ／…」「TORRAS/代理店ADEX」→ 先頭社
       (FB extract_client_name の '/' 分割と同じ流儀。'・' は ユニ・チャーム 等の
       社名内区切りなので分割しない)
    3. 末尾の括弧注記「ロート製薬（代理店：博報堂）」「ユニー（商業施設）」→ 除去
    4. 敬称「カゴメ様」「…さま」→ 除去
    5. 法人格 prefix/suffix「株式会社GA technologies」「TOTO株式会社」→ 除去
       (除去で空になる場合は除去前の値を保持)

    「東京都」「内閣府」等の官公庁名・「JCB」等の略称はそのまま返す。
    """
    s = (company or "").strip()
    if not s or s in _CLIENT_NAME_PLACEHOLDERS:
        return None

    # 複数社連記は先頭社を主クライアントとする
    s = re.split(r"[／/]", s, maxsplit=1)[0].strip()

    # 末尾の括弧注記を除去 (入れ子なし前提・複数回適用)
    while True:
        trimmed = _TRAILING_PAREN_RE.sub("", s).strip()
        if trimmed == s:
            break
        s = trimmed

    # 敬称 suffix
    for suffix in _HONORIFIC_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break

    # 法人格 prefix / suffix (両方付くことはない前提で各 1 回)
    stripped = s
    for affix in _CORPORATE_AFFIXES:
        if stripped.startswith(affix):
            stripped = stripped[len(affix) :].strip()
            break
    for affix in _CORPORATE_AFFIXES:
        if stripped.endswith(affix):
            stripped = stripped[: -len(affix)].strip()
            break
    if stripped:
        s = stripped

    return s or None


# ============================================================
# 事例集 corpus（📍ショート動画施策事例集・マスター表）の列写像 — B-10
# ============================================================
# 2026-09-11 追加。アポ前 事例ブリーフィング（pre_meeting_brief）が引く母集団を作る。
#
# ⚠️ 母集団の定義は **「yaml で case_corpus: "true" を宣言した gsheets spec の行」** であって
# 「ヘッダが事例っぽい行」ではない。pipeline 側は spec のフラグでしか本経路へ入らないので、
# 既存 2 シート（ナレッジ共有 / 営業 FB）には構造的に副作用が無い。下のコアヘッダ閾値は
# 「フラグ付きシートのヘッダが運用で差し替わったとき黙って誤写像しない」ための二次防御。
#
# 実ヘッダは未確定（マスター表の sheet_id 自体がユーザー探索中）。要望原文が名指しした
# 列は カテゴリ／企業名／商材／効果／営業担当 の 5 つで、「対外利用可否」列は追加可否を
# ユーザーが検討中。したがって:
#  - 列名は **表記ゆれを吸収して** 照合する（NFKC で全角/半角・丸数字・記号を潰し、
#    末尾の括弧注記を落としてから alias 表を引く）
#  - 「対外利用可否」列が**無い**ことを正常系として扱う（→ unknown。列が無いだけで
#    毎朝 ⚠ を全件に付けると狼少年化して本命の ⚠ が効かなくなる）
#
# 相互排他: 事例集のコアヘッダは FB（商流/顧客名/商談フェーズ…）ともナレッジ共有
# （正式社名/案件名/クライアント種別/提案プロダクト/資料の概要）とも 3 つ以上は交差しない。
# 「正式社名」だけは 企業名 の別名として受けるが、ナレッジ共有シートのコア一致数は 1 に
# とどまり閾値 3 に届かない（テストで固定）。
CASE_CORPUS_METADATA_KEY = "case_corpus"

# 3 値。ここ以外で文字列リテラルを増やさない。
CASE_EXTERNAL_USE_OK = "ok"
CASE_EXTERNAL_USE_NG = "ng"
CASE_EXTERNAL_USE_UNKNOWN = "unknown"

# 事例集の「カテゴリ」列（＝計画 §120 の業種）を載せる metadata key。
# 既存の業種フィルタは classify.as_metadata が cls_industry と industry の 2 本を書き、
# 検索側（adapters/pgvector_client.py の filter_industry）と slack_fb_parser の
# 集計はどちらも ``metadata->>'industry'`` を引く。事例集の同業種フォールバック
# （B-9 の list_case_studies(industry=...)）がこの既存規約に乗れるよう、
# 人手で書かれたカテゴリ列の値を **industry へも** 書く。B-9 は case_category を
# 直接知らなくてよい（この定数を import して使う契約）。
CASE_INDUSTRY_METADATA_KEY = "industry"
# 同値を Haiku 推定の cls_industry へも上書きする（人間入力 > 推定）。分類が OFF /
# 失敗した run でも industry が載ることを保証するのは pipeline 側の責務。
CASE_CLS_INDUSTRY_METADATA_KEY = "cls_industry"
CASE_CATEGORY_METADATA_KEY = "case_category"

# 表示用 note（朝の DM の ⚠ 注記にそのまま出る）の上限。
# spec_delta §3-4 は内部表現を「external_use_note（表示文言・スクラブ済み）」と
# 定義している。生セルには担当者名・他社名・URL が書かれうるので、metadata へ
# 入る時点で必ずスクラブする（消費側の善意に依存しない）。
CASE_EXTERNAL_USE_NOTE_MAX_LEN = 80

# canonical ラベル → metadata JSONB key。
# case_external_use_source は **生セル** で、pipeline が resolve_case_external_use を
# 通して case_external_use（3 値）＋ case_external_use_note（表示用の理由）へ置換する
# ＝ document の metadata に生セルがそのまま残ることはない。
_CASE_LABEL_TO_METADATA_KEY: dict[str, str] = {
    "企業名": "case_company",
    "カテゴリ": "case_category",
    "商材": "case_product",
    "効果": "case_effect",
    "営業担当": "case_owner",
    "対外利用可否": "case_external_use_source",
    "資料名": "case_asset_name",
}

# 実ヘッダのゆれ → canonical ラベル。NFKC 正規化＋括弧注記除去の **後** に引く。
_CASE_LABEL_ALIASES: dict[str, str] = {
    # 企業名
    "会社名": "企業名",
    "社名": "企業名",
    "正式社名": "企業名",
    "クライアント": "企業名",
    "クライアント名": "企業名",
    "得意先": "企業名",
    "得意先名": "企業名",
    "企業": "企業名",
    # カテゴリ（業種）
    "カテゴリー": "カテゴリ",
    "業種": "カテゴリ",
    "業界": "カテゴリ",
    "ジャンル": "カテゴリ",
    # 商材
    "商材名": "商材",
    "商品": "商材",
    "商品名": "商材",
    "プロダクト": "商材",
    "サービス": "商材",
    "施策名": "商材",
    # 効果
    "成果": "効果",
    "実績": "効果",
    "結果": "効果",
    "効果・成果": "効果",
    "効果/成果": "効果",
    "効果測定": "効果",
    # 営業担当
    "営業担当者": "営業担当",
    "担当": "営業担当",
    "担当者": "営業担当",
    "担当営業": "営業担当",
    "社内担当": "営業担当",
    # 対外利用可否
    "対外利用": "対外利用可否",
    "対外可否": "対外利用可否",
    "展開可否": "対外利用可否",
    "外部展開": "対外利用可否",
    "外部利用": "対外利用可否",
    "開示可否": "対外利用可否",
    "公開可否": "対外利用可否",
    "クライアント展開": "対外利用可否",
    "クライアント展開可否": "対外利用可否",
    # 資料名（NG 判定の名前シグナル源にもなる）
    "ファイル名": "資料名",
    "資料": "資料名",
    "保管先フォルダ": "資料名",
    "フォルダ名": "資料名",
}

# 事例集らしさのコア（FB / ナレッジ共有とは 3 つ以上交差しない）。
_CASE_CORE_LABELS = frozenset({"企業名", "カテゴリ", "商材", "効果", "営業担当"})
_CASE_MIN_CORE_HITS = 3

# 「対外利用可否」セルの値域。
# ⚠️ 非対称に見るのが安全装置の本体（2026-09-11 レビュー指摘 #1 の修正）:
#   - ng は **部分一致**（「NG（社外秘のため）」のような自由記述を拾う）
#   - ok は **完全一致ホワイトリスト**（部分一致だと否定・保留表現が ok へ倒れる）
# 旧実装は ok も部分一致だったため、ng マーカーに当たらない
#   「未公開」（"公開" を含む）/「公開前」/「可否未定」（"可" を含む）/「可否確認中」
# が全て ok になっていた＝まだ出せない事例が ⚠ なしで朝の DM に載る fail-OPEN。
# ok に当たらない語は **unknown**（＝「資料で確認」表示）へ倒す。unknown は安全側。
_CASE_NG_VALUE_MARKERS: tuple[str, ...] = (
    "ng",
    "不可",
    "×",
    "✕",
    "口頭",
    "社外秘",
    "非公開",
    "禁止",
    "confidential",
    "secret",
)
# ⚠️ **完全一致**（_normalize_case_value 後の値そのもの）でのみ ok。語を足すときは
# 「その語を含む否定・保留表現が存在しないか」ではなく「その語**そのもの**が
# 肯定か」だけ考えればよい＝部分一致時代の語彙事故が構造的に起きない。
_CASE_OK_VALUE_EXACT: frozenset[str] = frozenset(
    {
        "ok",
        "可",
        "○",
        "〇",
        "◯",
        "yes",
        "true",
        "社外提示ok",
        "公開可",
        "対外利用可",
    }
)

# フォルダ名・ファイル名・シート名に現れたら対外利用 NG と断じる語。
# 例: 「20260708_各社成功事例集★クライアント展開NG」「03｜事例（開示NG）」「口頭のみ」。
# 2026-09-11 追加（実見メモ case_corpus_columns_20260911.md）: 実データで最も多い
# 注意書きは「取扱注意」（例「20250618_フラットベース社の共有（取扱注意）」）で、
# 値マーカー側にしか無かった 社外秘 / confidential / 非公開 も名前で効かせる。
# 名前シグナルは ng 側にしか無い（ok へ倒す名前判定は存在しない）ので、語を足しても
# 単調性（ng は降格しない）は壊れない。
_CASE_NG_NAME_MARKERS: tuple[str, ...] = (
    "展開ng",
    "開示ng",
    "口頭",
    "取扱注意",
    "取り扱い注意",
    "confidential",
    "社外秘",
    "非公開",
)

# 末尾の括弧注記（「効果（数値）」「営業担当（社内）」等）。ヘッダ正規化で落とす。
_CASE_LABEL_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]\s*$")


def _normalize_case_label(label: str) -> str:
    """事例集シートのヘッダを canonical ラベルへ正規化する。

    ナレッジ共有（_normalize_form_label）と違い **NFKC を通す**。マスター表は人手で
    作られた表で、全角英数・全角スペース・半角カナ・丸括弧のゆれが列名に入りうるため。
    正規化順: NFKC → 全角/半角スペース除去 → 末尾括弧注記除去 → alias。
    """
    normalized = unicodedata.normalize("NFKC", label or "")
    normalized = normalized.replace("　", " ").strip()
    # 「効果（数値）」→「効果」。入れ子なし前提・複数回適用。
    while True:
        trimmed = _CASE_LABEL_PAREN_RE.sub("", normalized).strip()
        if trimmed == normalized:
            break
        normalized = trimmed
    # 内部の空白・中黒は列名のゆれなので潰す（「営業 担当」「効果 ・ 成果」）。
    normalized = re.sub(r"\s+", "", normalized)
    return _CASE_LABEL_ALIASES.get(normalized, normalized)


def _normalize_case_value(value: str | None) -> str:
    """セル値を照合用に正規化する（NFKC → 空白除去 → casefold）。"""
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"\s+", "", normalized.replace("　", " "))
    return normalized.casefold()


def map_case_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """事例集マスター表の ヘッダ → 値 を metadata JSONB 用 dict へ写像する。

    Returns:
        - 正規化後のコアヘッダが _CASE_MIN_CORE_HITS 個以上あれば
          metadata key → 非空値 の dict（空セルの列は含めない）
        - コアヘッダ不足（= 事例集マスター表ではない）なら空 dict {}
          → ナレッジ共有 / 営業 FB / 任意シートへの副作用ゼロ（テストで固定）

    ``case_external_use_source`` は生セル。3 値化は resolve_case_external_use が行う。
    """
    if not fields:
        return {}

    normalized: dict[str, str] = {}
    for label, value in fields.items():
        canonical = _normalize_case_label(label)
        # 表記ゆれで同一 canonical に潰れた場合は非空値を優先（空値で上書きしない）
        if canonical not in normalized or (value or "").strip():
            normalized[canonical] = value or ""

    core_hits = len(normalized.keys() & _CASE_CORE_LABELS)
    if core_hits < _CASE_MIN_CORE_HITS:
        return {}

    out: dict[str, str] = {}
    for canonical, value in normalized.items():
        key = _CASE_LABEL_TO_METADATA_KEY.get(canonical)
        if key is None:
            continue
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        out[key] = cleaned
    return out


def normalize_case_external_use(value: str | None) -> str:
    """「対外利用可否」セルを 3 値（ok / ng / unknown）へ正規化する。

    空セル・未知語は **unknown**（「列が無い/書かれていない」を ng にも ok にも倒さない）。

    判定は非対称:
      1. ng マーカーの **部分一致**（「NG（社外秘）」等の自由記述を拾う）
      2. ok は **完全一致ホワイトリスト**のみ
      3. どちらでもなければ unknown

    2 を部分一致に緩めると「未公開」「公開前」「可否未定」「可否確認中」が ok へ倒れる
    （いずれも ng マーカーに当たらない）。この関数はこの PR の唯一の安全装置なので、
    判定不能は必ず unknown（＝「資料で確認」）へ落とす。
    """
    normalized = _normalize_case_value(value)
    if not normalized:
        return CASE_EXTERNAL_USE_UNKNOWN
    if any(marker in normalized for marker in _CASE_NG_VALUE_MARKERS):
        return CASE_EXTERNAL_USE_NG
    if normalized in _CASE_OK_VALUE_EXACT:
        return CASE_EXTERNAL_USE_OK
    return CASE_EXTERNAL_USE_UNKNOWN


def find_case_ng_name(names: Sequence[str | None]) -> str | None:
    """フォルダ名 / ファイル名 / シート名に NG 語（展開NG・開示NG・口頭）があれば返す。

    返り値は **マッチした名前そのもの**（表示用の理由文言に使う）。無ければ None。
    """
    for name in names:
        if not name:
            continue
        haystack = _normalize_case_value(name)
        if any(marker in haystack for marker in _CASE_NG_NAME_MARKERS):
            return name.strip()
    return None


_CASE_NOTE_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CASE_NOTE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def scrub_case_external_use_note(raw: str | None) -> str | None:
    """表示用 note を作る（**必ずスクラブ済み**であることがこの関数の不変条件）。

    note は朝の DM の ⚠ 注記としてそのまま人目に出る。生セル／フォルダ名には
    URL・メールアドレス・改行混じりの長文が入りうるので、metadata へ載せる前に:

      1. URL（https?:// … / www. …）とメールアドレスを除去
      2. 改行・タブ・連続空白を 1 個の半角空白へ潰す
      3. CASE_EXTERNAL_USE_NOTE_MAX_LEN 文字で打ち切り（末尾に「…」）

    他社名・担当者名は語彙が閉じないので機械的には落とせない。落とせない分は
    B-9 側の表示で「事例の出典名」として扱う（PR 本文に明記）。
    空になったら None（＝注記なし）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    text = _CASE_NOTE_URL_RE.sub("", text)
    text = _CASE_NOTE_EMAIL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text.replace("　", " ")).strip()
    if not text:
        return None
    if len(text) > CASE_EXTERNAL_USE_NOTE_MAX_LEN:
        text = text[: CASE_EXTERNAL_USE_NOTE_MAX_LEN - 1].rstrip() + "…"
    return text or None


class CaseExternalUse(NamedTuple):
    """対外利用可否の判定結果。

    value: ok / ng / unknown
    note:  表示用の理由（スクラブ済み）。理由が無ければ None。
           **生セルそのものではない**（scrub_case_external_use_note を必ず通す）。
    """

    value: str
    note: str | None


def resolve_case_external_use(
    *,
    column_value: str | None = None,
    names: Sequence[str | None] = (),
    previous: str | None = None,
    previous_note: str | None = None,
) -> CaseExternalUse:
    """対外利用可否を決める（**単調**: 一度 ng になったら降格しない）。

    評価順を固定する:
      1. 保存済みが ng → ng（**sticky**。再取込で列が消えても・名前が変わっても戻さない。
         documents.metadata は upsert で全置換されるため、ここで持ち上げないと
         「⚠なしの NG 事例」が翌朝の DM に載る）
      2. 名前（シート名 / タブ名 / 資料名）に 展開NG・開示NG・口頭 → ng
      3. 列の値 → ok / ng / unknown
      4. 列が無い / 空 → unknown

    ok は sticky にしない（unknown へ落ちるのは安全側の劣化なので許す）。
    """
    if normalize_case_external_use(previous) == CASE_EXTERNAL_USE_NG:
        # previous_note は前回この関数がスクラブして書いた値だが、DB 由来の入力なので
        # ここでももう一度通す（「note は常にスクラブ済み」を単一地点で保証する）。
        return CaseExternalUse(CASE_EXTERNAL_USE_NG, scrub_case_external_use_note(previous_note))

    ng_name = find_case_ng_name(names)
    if ng_name is not None:
        return CaseExternalUse(CASE_EXTERNAL_USE_NG, scrub_case_external_use_note(ng_name))

    value = normalize_case_external_use(column_value)
    return CaseExternalUse(value, scrub_case_external_use_note(column_value))
