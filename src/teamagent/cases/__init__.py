"""事例レコード層（Vault 文書 → 構造化された施策事例）。

設計: docs/… ではなく Artifacts/aico-inputs-20260911/case_knowledge_design_20260916.md（v2）。
Vault の documents は消さず、抽出した事例レコード（``case_records``）が元文書への参照を持つ。
検索は「構造（sector/purpose/product_state/traits）で絞る → 要約埋め込みで並べる」。

- schema.py   : ``CaseRecord`` v1 と tag 語彙（青木 事例DB.json の ``_meta.tag_vocab`` を写す）
- extract.py  : Bedrock Converse で 1 文書 → n 事例（JSON のみ・1 回 repair・語彙外は その他）
- store.py    : ``case_records`` テーブルの upsert / 構造一致＋類似度検索（SQL はここだけ）
"""
