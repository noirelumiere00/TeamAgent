"""テンプレ（boilerplate）検出: コーパス横断で重複する定型 chunk に印を付ける。

営業資料には会社概要・免責事項・問い合わせ先など、複数の別資料に**同一テキスト**で
出現する「テンプレ箇所」が多い。これらは検索でノイズになり、本質的な提案内容を
埋もれさせる。本モジュールはコーパス全体を走査し、同じ正規化テキストが
``min_docs`` 件以上の**別 document** に出現する chunk を boilerplate と判定して
``chunks.metadata`` の boolean キー ``boilerplate`` に印を付ける。

設計（全 agent 共通契約）:
- テンプレ印 = ``chunks.metadata`` (JSONB) の boolean キー ``boilerplate``=true。
  ``metadata`` は title_only chunk 等が使う既存 JSONB 列なので **DB migration 不要**。
- 指紋 = 正規化テキストの md5 を **SQL 内で算出**する。正規化 =
  ``lower(btrim(content))`` して連続空白を 1 個に圧縮
  （``md5(regexp_replace(lower(btrim(content)), '\\s+', ' ', 'g'))``）。
  chunk への事前保存は不要（評価のたびに SQL で算出）。
- テンプレ判定 = その正規化指紋が ``min_docs`` 件以上の**別 document** に
  出現していれば boilerplate（``COUNT(DISTINCT document_id) >= min_docs``）。
- 冪等＆自己修正: 閾値を**下回った**指紋の chunk からは ``boilerplate`` キーを
  除去する。再取込のたびに再評価され、追加資料の同テンプレ箇所も自動でフラグされ、
  資料が消えてテンプレでなくなった箇所は自動で解除される。

本関数は env を読まない純 I/O（1 コネクションを受け取る）。env ゲート
（``BOILERPLATE_DETECT`` 等）の判定は呼び出し側（pipeline.py）が行う。
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# 正規化指紋: lower → btrim → 連続空白を 1 個に圧縮 → md5。
# 列名（content）・テーブル名（chunks）はコード内固定なので bandit B608 は非該当。
# 唯一の動的値 min_docs は placeholder（%s）で bind するため injection 不可。
_NORMALIZED_FINGERPRINT_SQL = "md5(regexp_replace(lower(btrim(content)), '\\s+', ' ', 'g'))"

# 正規化テキストの文字数（M2 の最小文字数ガード用）。指紋と同じ正規化を char_length で測る。
_NORMALIZED_LENGTH_SQL = "char_length(regexp_replace(lower(btrim(content)), '\\s+', ' ', 'g'))"


def mark_boilerplate(conn: Any, *, min_docs: int, min_chars: int = 40) -> int:
    """コーパス全体の chunks にテンプレ印（``metadata.boilerplate``）を付け直す。

    正規化指紋ごとに ``COUNT(DISTINCT document_id)`` を数え、``min_docs`` 件以上の
    別 document に出現する指紋を boilerplate とみなす。該当 chunk の ``metadata`` に
    ``{"boilerplate": true}`` を ``jsonb_set`` で付与し、閾値を下回った指紋の chunk
    からは ``metadata - 'boilerplate'`` でキーを除去する（冪等＆自己修正）。

    M2（短 chunk ガード）: 正規化テキストの文字数が ``min_chars`` 未満の chunk は
    テンプレ指紋の集計から除外する。ページ番号・連番・「以上」等の短い全文一致は
    複数資料に偶然並ぶだけで本質的なテンプレではなく、これを boilerplate 判定すると
    その短文しか持たない chunk（＝唯一の回答源）を検索から消してしまうため。

    M3（suppressed 除外）: dedup（docdedup）で非正本（``documents.metadata.suppressed='true'``）
    と確定した document の chunk は ``COUNT(DISTINCT document_id)`` の母数から外す。
    同一資料の重複コピーで DISTINCT document_id が水増しされ、テンプレでない本文まで
    閾値超えで boilerplate 化するのを防ぐ。run 内の実行順は dedup→boilerplate（pipeline.py）。

    Args:
        conn: psycopg コネクション（admin role で chunks を UPDATE できること）。
            本関数はトランザクション境界を持たない＝呼び出し側の ``connection()``
            コンテキストマネージャが commit / rollback を担う。
        min_docs: テンプレ判定の閾値（この件数以上の別 document に出現でテンプレ）。
        min_chars: 正規化テキストの最小文字数。これ未満の短い chunk はテンプレ判定の
            対象外（M2）。0 以下なら実質ガード無効（後方互換）。

    Returns:
        実際に変化した（印を付けた or 外した）chunk 数（ログ用）。
    """
    if min_docs < 1:
        # 閾値が無意味なら何もしない（fail-safe・除去だけ走らせると全解除になり危険）。
        logger.warning("boilerplate_min_docs_invalid", min_docs=min_docs)
        return 0

    # 1) boilerplate 指紋集合（min_docs 件以上の別 document に出現する正規化指紋）。
    #    両 UPDATE で共有するため CTE で 1 回だけ算出する。
    #    M2: 正規化長 < min_chars の短い chunk は集計から除外（短文をテンプレ化しない）。
    #    M3: dedup で suppressed（非正本）と確定した document は DISTINCT 母数から除外
    #        （重複コピーで document_id を水増ししない）。実行順は dedup→boilerplate。
    #    placeholder 順 = (min_chars, min_docs)。両 UPDATE で同じ順序で bind する。
    boilerplate_fps_cte = f"""
        WITH boilerplate_fps AS (
            SELECT {_NORMALIZED_FINGERPRINT_SQL} AS fp
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {_NORMALIZED_LENGTH_SQL} >= %s
              AND COALESCE(d.metadata->>'suppressed', '') IS DISTINCT FROM 'true'
            GROUP BY 1
            HAVING COUNT(DISTINCT c.document_id) >= %s
        )
    """  # nosec B608  # 列/テーブル固定・min_chars/min_docs は placeholder（下で bind）

    # 2) 付与: boilerplate 指紋に属し、まだ boilerplate=true でない chunk に印を付ける。
    #    既に true の行は除外＝冪等（無変更行を rowcount に数えない）。
    add_sql = (
        boilerplate_fps_cte
        + f"""
        UPDATE chunks
        SET metadata = jsonb_set(
            COALESCE(metadata, '{{}}'::jsonb), '{{boilerplate}}', 'true'::jsonb, true
        )
        WHERE {_NORMALIZED_FINGERPRINT_SQL} IN (SELECT fp FROM boilerplate_fps)
          AND COALESCE((metadata->>'boilerplate')::boolean, false) IS DISTINCT FROM true
        """  # nosec B608  # 列/テーブル固定・min_docs は placeholder
    )

    # 3) 除去: boilerplate=true が付いているが、もはや boilerplate 指紋でない chunk から
    #    キーを外す（資料が減ってテンプレでなくなった箇所の自己修正）。
    remove_sql = (
        boilerplate_fps_cte
        + f"""
        UPDATE chunks
        SET metadata = metadata - 'boilerplate'
        WHERE metadata ? 'boilerplate'
          AND {_NORMALIZED_FINGERPRINT_SQL} NOT IN (SELECT fp FROM boilerplate_fps)
        """  # nosec B608  # 列/テーブル固定・min_docs は placeholder
    )

    # CTE の placeholder 順 = (min_chars, min_docs)。両 UPDATE で同じ順で bind。
    affected = 0
    with conn.cursor() as cur:
        cur.execute(add_sql, (min_chars, min_docs))
        added = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur.execute(remove_sql, (min_chars, min_docs))
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        affected = added + removed

    logger.info(
        "boilerplate_marked",
        min_docs=min_docs,
        min_chars=min_chars,
        added=added,
        removed=removed,
        affected=affected,
    )
    return affected
