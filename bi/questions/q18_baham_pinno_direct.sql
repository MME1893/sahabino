-- Pair comparison is NOT a category benchmark; same UTC day + historical locale only.
WITH ranked AS (
 SELECT a.package_name,a.name AS application_name,ct.country_code,ct.language_code,
 (s.collected_at AT TIME ZONE 'UTC')::date AS snapshot_day_utc,s.collected_at,s.id,s.score,
 ROW_NUMBER() OVER(PARTITION BY a.package_name,ct.country_code,ct.language_code,(s.collected_at AT TIME ZONE 'UTC')::date
 ORDER BY s.collected_at DESC,s.id DESC) rn
 FROM public.playstore_app_snapshots s JOIN public.applications a ON a.id=s.application_id
 JOIN public.crawl_tasks ct ON ct.id=s.crawl_task_id AND ct.application_id=s.application_id
 WHERE a.package_name IN('ir.android.baham','app.pinno') AND ct.task_type='app_details' AND ct.status='succeeded'
 [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
), daily AS (SELECT * FROM ranked WHERE rn=1)
SELECT b.snapshot_day_utc,b.country_code AS crawl_country_code,b.language_code AS crawl_language_code,
 b.score::numeric AS baham_store_score_0_to_5,p.score::numeric AS pinno_store_score_0_to_5,
 (b.score-p.score)::numeric AS baham_minus_pinno_score_points,
 b.collected_at AS baham_collected_at,p.collected_at AS pinno_collected_at
FROM daily b JOIN daily p ON p.snapshot_day_utc=b.snapshot_day_utc AND p.country_code=b.country_code
 AND p.language_code=b.language_code AND p.package_name='app.pinno'
WHERE b.package_name='ir.android.baham'
 [[ AND b.snapshot_day_utc >= {{start_date}} ]] [[ AND b.snapshot_day_utc <= {{end_date}} ]]
ORDER BY b.snapshot_day_utc,b.country_code,b.language_code;
