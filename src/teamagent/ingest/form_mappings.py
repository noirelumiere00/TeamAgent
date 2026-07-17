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
from collections.abc import Mapping

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
