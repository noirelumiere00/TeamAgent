"""差分取り込み（``INGEST_DIFFERENTIAL``・既定 OFF）用の内容ハッシュ。

背景（2026-08 実測）: 平日 18:00 の ingest は前日から変わっていない文書
（gsheets 746-749 件/日 + slack 198 件/日 ≒ 947 件/日）を毎回 upsert し直し、
その全件で Haiku 分類（bedrock_converse ≈950 回/run・月約 $40）を再実行していた。
本モジュールは「分類・embedding・upsert の入力になるものすべて」を正規化した
sha256 を計算し、``documents.metadata['content_sha256']`` に保存した前回値と
一致した文書の再処理（Bedrock 分類・embedding・chunks 再書込・upsert）を
丸ごとスキップ可能にする。

gdrive バイナリ経路の ``USE_UNCHANGED_SKIP``（Drive 提供の ``md5_checksum`` を
metadata に保存して照合・pipeline.py ``_should_skip_unchanged_gdrive_file``）と
同じ「metadata に指紋を保存して次回照合」流儀。slack / gsheets はソース側の
checksum が無いため、入力から自前で計算する。

設計原則:

- ハッシュ対象は **入力のみ**（本文・タイトル・ACL・入力 metadata・modified_at・
  source_uri）。分類出力（``cls_*``）とハッシュ自身は対象にしない
  （出力を入力に混ぜると、分類結果の揺れだけで永遠に不一致になる）。
- ACL（``acl_emails`` / ``acl_groups`` / ``owner_email``）を必ず含める。
  「本文が同じだから」と ACL 差分をスキップすると権限変更が DB に反映されない。
- 出力へ影響する実行時設定（分類 ON/OFF・contextualize ON/OFF・embedder backend）
  を ``pipeline_config`` として含める。設定を切り替えた run では全文書のハッシュが
  変わり、確実に再処理される（黙った取りこぼしを防ぐ）。
- ``_HASH_SCHEMA_VERSION`` を上げると全ハッシュが無効化され、次 run で全件が
  1 回だけ再処理される（ハッシュ対象の変更・embedding モデル差し替え等、
  入力以外の意味変更時の escape hatch）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

# documents.metadata に保存するキー（gdrive 経路の 'md5_checksum' と同流儀の別キー）。
INGEST_CONTENT_HASH_KEY = "content_sha256"

# ハッシュ計算の意味が変わったら上げる（全文書が次 run で 1 回だけ再処理される）。
_HASH_SCHEMA_VERSION = "1"


def compute_document_content_hash(
    *,
    source_type: str,
    external_id: str,
    text: str,
    title: str | None,
    source_uri: str | None,
    owner_email: str,
    acl_emails: Sequence[str],
    acl_groups: Sequence[str],
    metadata: Mapping[str, Any],
    modified_at: str | None,
    pipeline_config: Mapping[str, Any] | None = None,
) -> str:
    """1 document 分の入力を正規化して sha256 hex を返す。

    ``metadata`` には「分類出力（``cls_*``）とハッシュキーを含まない入力 metadata」を
    渡すこと（呼び出し側がハッシュ計算を分類より前に置いて構造的に保証する）。
    防御として、ここでも ``cls_*`` / ``content_sha256`` が混入していたら除外する
    （既存 metadata の引き回しバグでスキップ判定が歪むのを防ぐ二重壁）。

    正規化: dict はキー昇順（``sort_keys``）・ACL 配列は要素昇順で並べ、挿入順や
    解決順の揺れが偽の「変更あり」を作らないようにする。
    """
    normalized_metadata = {
        str(key): value
        for key, value in metadata.items()
        if not str(key).startswith("cls_") and str(key) != INGEST_CONTENT_HASH_KEY
    }
    payload = {
        "hash_schema_version": _HASH_SCHEMA_VERSION,
        "source_type": source_type,
        "external_id": external_id,
        "text": text,
        "title": title,
        "source_uri": source_uri,
        "owner_email": owner_email,
        "acl_emails": sorted(str(email) for email in acl_emails),
        "acl_groups": sorted(str(group) for group in acl_groups),
        "metadata": normalized_metadata,
        "modified_at": modified_at,
        "pipeline_config": dict(pipeline_config or {}),
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
