-- Current registry category, not historical category. Missing snapshots remain missing.
WITH portfolio AS (
 SELECT a.id, a.package_name, c.code AS primary_category_code,c.name AS primary_category_name
 FROM public.applications a
 LEFT JOIN LATERAL (SELECT ac.category_id FROM public.application_categories ac
    WHERE ac.application_id=a.id AND ac.is_primary IS TRUE ORDER BY ac.category_id LIMIT 1) pc ON TRUE
 LEFT JOIN public.categories c ON c.id=pc.category_id
 WHERE a.package_name <> 'ir.rightel.myrightel'
 [[ AND a.package_name={{application}} ]] [[ AND c.code={{category}} ]]
), latest AS (
 SELECT p.*, x.score,x.collected_at
 FROM portfolio p LEFT JOIN LATERAL (
   SELECT s.score,s.collected_at FROM public.playstore_app_snapshots s
   JOIN public.crawl_tasks ct ON ct.id=s.crawl_task_id AND ct.application_id=s.application_id
   WHERE s.application_id=p.id AND ct.task_type='app_details' AND ct.status='succeeded'
   [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
   ORDER BY s.collected_at DESC,s.id DESC LIMIT 1
 ) x ON TRUE
)
SELECT primary_category_code,primary_category_name,COUNT(*) AS application_count,
 COUNT(collected_at) AS applications_with_snapshot,AVG(score)::numeric AS mean_latest_store_score_0_to_5,
 MIN(collected_at) AS oldest_latest_snapshot_at,MAX(collected_at) AS newest_latest_snapshot_at
FROM latest GROUP BY primary_category_code,primary_category_name
ORDER BY primary_category_name NULLS LAST;
