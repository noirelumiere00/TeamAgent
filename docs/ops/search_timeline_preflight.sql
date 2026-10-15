-- Claude が有効化前に、本人と同じ RLS・ACL の読み取り接続で実行する。
-- psql の変数 client に対象取引先の正確な表記を渡す。秘密情報・接続設定はこのファイルに置かない。
-- ingested_at は初回取り込みなので、直近の同期成功時刻とは比較しない。
BEGIN READ ONLY;

SELECT id, source_type, external_id, title, source_uri,
       modified_at AT TIME ZONE INTERVAL '+09:00' AS modified_at_jst,
       ingested_at AT TIME ZONE INTERVAL '+09:00' AS first_ingested_at_jst,
       metadata->>'client_name' AS client_name,
       metadata->>'advertiser' AS advertiser,
       metadata->>'cls_project' AS cls_project,
       metadata->>'campaign' AS campaign,
       metadata->>'case_product' AS case_product,
       metadata->>'proposed_menu' AS proposed_menu,
       metadata->>'cls_phase' AS cls_phase,
       metadata->>'deal_phase' AS deal_phase,
       metadata->>'topic' AS topic,
       metadata->>'is_sales_fb' AS is_sales_fb,
       metadata->>'campaign_aggregate' AS campaign_aggregate,
       metadata->>'video_count' AS video_count,
       metadata->>'first_posted_on' AS first_posted_on,
       metadata->>'last_posted_on' AS last_posted_on
FROM documents
WHERE metadata->>'suppressed' IS DISTINCT FROM 'true'
  AND (metadata->>'client_name' = :'client'
       OR metadata->>'advertiser' = :'client'
       OR metadata->>'cls_project' = :'client'
       OR position(:'client' IN title) > 0)
ORDER BY modified_at DESC NULLS LAST, id;

-- 投稿実績が 2 施策とも存在し、本数・投稿期間が欠けず、別施策・重複行でないか。
SELECT metadata->>'advertiser' AS advertiser, metadata->>'campaign' AS campaign,
       count(*) AS document_count,
       array_agg(metadata->>'video_count') AS video_counts,
       array_agg(metadata->>'first_posted_on') AS first_post_dates,
       array_agg(metadata->>'last_posted_on') AS last_post_dates,
       count(*) FILTER (WHERE modified_at IS NULL) AS missing_modified_at
FROM documents
WHERE metadata->>'campaign_aggregate' = 'true'
  AND (metadata->>'advertiser' = :'client' OR metadata->>'client_name' = :'client')
  AND metadata->>'suppressed' IS DISTINCT FROM 'true'
GROUP BY metadata->>'advertiser', metadata->>'campaign';

-- チャンクだけに日付・施策識別子が残っていないか。状態判定は documents.metadata を使う。
SELECT d.id AS document_id, c.chunk_idx, c.metadata
FROM documents d JOIN chunks c ON c.document_id = d.id
WHERE (d.metadata->>'client_name' = :'client' OR d.metadata->>'advertiser' = :'client')
  AND c.metadata <> '{}'::jsonb
ORDER BY d.id, c.chunk_idx;

ROLLBACK;
