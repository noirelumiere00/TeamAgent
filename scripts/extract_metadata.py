"""提案 PDF からメタデータを Claude Sonnet 4.6 で JSON 抽出するパイプライン。

各 PDF（同じ file_name の chunks を結合）に対して以下を抽出：
- industry: 業界カテゴリ（飲食/化粧品/エネルギー/不動産/...）
- client_company: 提案先企業名
- vendor_company: 提案元企業名（基本「ベクトル」「株式会社ベクトル」）
- target_audience: ターゲット層（Z世代/30代女性/BtoB 担当者 等）
- budget: 予算（金額 or 規模感）
- service_type: 提供サービス（ショート動画施策/PR/MEO/...）
- key_keywords: 重要キーワード（上位 5 件）
- proposed_at: 提案日付（YYYY-MM-DD or YYYY-MM）

結果は proposals_chunks / proposals_chunks_contextual の metadata JSONB 列に保存し、
filter_industry 等のフィルタが効くようにする。

Usage:
    python scripts/extract_metadata.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import psycopg  # noqa: E402

from teamagent.adapters.bedrock_client import BedrockClient  # noqa: E402

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)
SONNET_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)
TABLES = ["proposals_chunks", "proposals_chunks_contextual"]

# JSON Schema を Claude に直接渡してフォーマット指定
EXTRACTION_INSTRUCTION = """以下は社内提案書 PDF の全文です。提案書から以下のメタデータを抽出して、JSON で返してください。

<document>
{document}
</document>

抽出する項目（記載がない場合は null）:

## 基本メタデータ
- industry: 業界カテゴリ（飲食 / 化粧品 / エネルギー / 不動産 / 自治体 / 製造業 / 教育 / 医療 / IT / 小売 / 金融 / 旅行 / メディア / その他）
- client_company: 提案先企業名（例: 株式会社INPEX、森ビル株式会社）
- vendor_company: 提案元企業名（基本「株式会社ベクトル」または「ベクトル」）
- target_audience: ターゲット層（例: Z世代、BtoB 担当者、ヴェネチアン来訪客、30代女性）
- budget: 予算情報（記載があれば金額 or 規模感、なければ null）
- service_type: 提供サービス（例: ショート動画施策、PR 代行、MEO、提案コンテンツ生成）
- key_keywords: この提案を象徴するキーワード上位 5 件（リスト）
- proposed_at: 提案日付（YYYY-MM-DD or YYYY-MM、不明なら null）

## 文脈設計フレーム（高林構想ベース）
- media: 提案で言及している主要メディア（例: TikTok / Instagram Reels / YouTube Shorts / X / LINE / Threads / 自社サイト）。最大 5 個のリスト
- communities: 提案で言及している界隈・コミュニティ（例: Z世代女子 / 美容クラスタ / B2B 投資家 / 動画クリエイター）。最大 5 個のリスト
- frequent_words: 提案内で頻出する象徴的なワード（例: アルゴリズム / バズ / トレンド / UGC）。最大 5 個のリスト
- right_brain_appeal: 右脳訴求のキーワード（感情・共感・印象。例: 親しみ / 安心 / ワクワク / 圧倒される映像美）。最大 3 個のリスト or null
- left_brain_appeal: 左脳訴求のキーワード（論理・データ・コスト。例: ROI / リーチ数 / 視聴単価 / CTR）。最大 3 個のリスト or null
- pitch_axis: 訴求軸（提案の核となるメッセージ。例: 「視聴単価が最も安いプラットフォーム」「美術館来訪を 30 代女性に拡げる」）。1 文の文字列 or null
- expression: 表現手法（例: ストーリー仕立て / マルチコンテキスト / VVS / ワンメッセージ / 切り抜き量産）。最大 3 個のリスト or null

回答ルール:
1. 必ず JSON だけを返す（説明文や前置きなし）
2. JSON は ```json ブロックに入れない、生の JSON だけ
3. 日本語のまま、文字列フィールドは double-quote
4. industry は上記リストから選ぶ、リストに無ければ「その他」
5. 各リスト系フィールドは [] や null（記載なし）でも OK
"""


def fetch_documents(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """file_name ごとに全 chunks を結合した document_text を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT file_name, string_agg(text, E'\\n\\n' ORDER BY page_num, chunk_idx) AS doc_text
            FROM proposals_chunks
            GROUP BY file_name
            """
        )
        rows = cur.fetchall()
    return {r[0]: r[1] for r in rows}


def extract_metadata(
    bedrock: BedrockClient, document_text: str, request_id: str
) -> tuple[dict[str, Any], float]:
    """Sonnet 4.6 にメタデータ抽出を依頼。結果 JSON と概算コストを返す。"""
    prompt = EXTRACTION_INSTRUCTION.format(document=document_text)
    resp = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        request_id=request_id,
        temperature=0.1,
        max_tokens=2000,
    )
    raw = resp.text.strip()
    # 念のため ```json で囲まれていたら剥がす
    if raw.startswith("```"):
        raw = raw.lstrip("`").lstrip("json").strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ⚠️ JSON parse 失敗: {e}")
        print(f"     応答（先頭 300 字）: {raw[:300]}")
        metadata = {"_parse_error": str(e), "_raw": raw[:500]}
    return metadata, resp.usage.cost_usd


def update_metadata(
    conn: psycopg.Connection[Any], file_name: str, metadata: dict[str, Any]
) -> int:
    """両テーブルに metadata JSONB を UPDATE する。"""
    total = 0
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    with conn.cursor() as cur:
        for table in TABLES:
            # metadata JSONB 列を ADD（IF NOT EXISTS）
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS metadata JSONB;"
            )
            cur.execute(
                f"UPDATE {table} SET metadata = %s::jsonb WHERE file_name = %s;",
                (metadata_json, file_name),
            )
            total += cur.rowcount
    conn.commit()
    return total


def main() -> int:
    print("🚀 メタデータ抽出パイプライン開始")
    print(f"  source: proposals_chunks")
    print(f"  model: {SONNET_MODEL_ID}")

    bedrock = BedrockClient(
        region=os.environ.get("AWS_REGION", "us-east-1"),
        model_id=SONNET_MODEL_ID,
    )

    conn = psycopg.connect(DB_DSN)
    try:
        documents = fetch_documents(conn)
        print(f"📊 対象: {len(documents)} files")

        total_cost = 0.0
        start = time.perf_counter()
        for i, (file_name, doc_text) in enumerate(documents.items(), 1):
            req_id = f"meta-{i:02d}-{file_name[:10]}"
            print(f"\n📄 [{i}/{len(documents)}] {file_name} ({len(doc_text)} chars)")
            try:
                metadata, cost = extract_metadata(bedrock, doc_text, req_id)
            except Exception as e:
                print(f"  ❌ Bedrock 呼び出し失敗: {e}")
                continue

            print(f"  抽出結果:")
            print(f"    industry         = {metadata.get('industry')!r}")
            print(f"    client_company   = {metadata.get('client_company')!r}")
            print(f"    target_audience  = {metadata.get('target_audience')!r}")
            print(f"    service_type     = {metadata.get('service_type')!r}")
            print(f"    proposed_at      = {metadata.get('proposed_at')!r}")
            print(f"    key_keywords     = {metadata.get('key_keywords')!r}")
            print(f"    -- 文脈設計フレーム --")
            print(f"    media            = {metadata.get('media')!r}")
            print(f"    communities      = {metadata.get('communities')!r}")
            print(f"    frequent_words   = {metadata.get('frequent_words')!r}")
            print(f"    right_brain      = {metadata.get('right_brain_appeal')!r}")
            print(f"    left_brain       = {metadata.get('left_brain_appeal')!r}")
            print(f"    pitch_axis       = {metadata.get('pitch_axis')!r}")
            print(f"    expression       = {metadata.get('expression')!r}")
            print(f"    cost             = ${cost:.4f}")

            updated = update_metadata(conn, file_name, metadata)
            print(f"  ✅ 両テーブル合計 {updated} 行を更新")
            total_cost += cost

        elapsed = time.perf_counter() - start
        print(f"\n🎉 完了：{len(documents)} files / {elapsed:.1f}秒 / 総コスト ${total_cost:.4f}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
