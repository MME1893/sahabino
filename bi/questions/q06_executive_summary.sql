-- Portfolio overview: unfiltered registry population; independently count valid snapshots.
WITH population AS (
 SELECT a.id, a.name, a.package_name FROM public.applications a
 WHERE a.package_name <> 'ir.rightel.myrightel'
 [[ AND a.package_name = {{application}} ]]
), latest AS (
 SELECT p.id, s.collected_at, s.score, s.ratings_count, s.reviews_count
 FROM population p LEFT JOIN LATERAL (
   SELECT s.collected_at,s.score,s.ratings_count,s.reviews_count
   FROM public.playstore_app_snapshots s
   JOIN public.crawl_tasks ct ON ct.id=s.crawl_task_id AND ct.application_id=s.application_id
   WHERE s.application_id=p.id AND ct.task_type='app_details' AND ct.status='succeeded'
   [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
   ORDER BY s.collected_at DESC,s.id DESC LIMIT 1
 ) s ON TRUE
)
SELECT COUNT(*) AS portfolio_application_count,
 COUNT(collected_at) AS applications_with_snapshot,
 COUNT(*)-COUNT(collected_at) AS applications_missing_snapshot,
 MAX(collected_at) AS newest_snapshot_at,
 MIN(collected_at) AS oldest_latest_snapshot_at,
 EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MAX(collected_at)))/3600.0 AS newest_snapshot_age_hours,
 EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MIN(collected_at)))/3600.0 AS oldest_latest_snapshot_age_hours
FROM latest;
