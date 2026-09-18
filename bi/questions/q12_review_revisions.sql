-- Consecutive score transitions ordered by observation timestamp + composite-PK task ID.
WITH observed AS (
 SELECT r.id AS review_id,a.name AS application_name,a.package_name,o.crawl_task_id,o.observed_at,
 ct.country_code AS crawl_country_code,ct.language_code AS crawl_language_code,o.score
 FROM public.review_observations o JOIN public.reviews r ON r.id=o.review_id
 JOIN public.applications a ON a.id=r.application_id
 JOIN public.crawl_tasks ct ON ct.id=o.crawl_task_id AND ct.application_id=r.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='reviews' AND ct.status='succeeded'
 [[ AND a.package_name={{application}} ]]
), sequenced AS (
 SELECT observed.*,LAG(score) OVER w AS previous_score
 FROM observed WINDOW w AS(PARTITION BY review_id ORDER BY observed_at,crawl_task_id)
), transitions AS (
 SELECT *, (observed_at AT TIME ZONE 'UTC')::date AS transition_day_utc
 FROM sequenced WHERE previous_score IS NOT NULL AND previous_score<>score
)
SELECT application_name,package_name,crawl_country_code,crawl_language_code,transition_day_utc,
 COUNT(*) AS score_revision_count,
 COUNT(*) FILTER(WHERE score>previous_score) AS score_upgrade_count,
 COUNT(*) FILTER(WHERE score<previous_score) AS score_downgrade_count
FROM transitions WHERE TRUE
 [[ AND crawl_country_code={{country}} ]] [[ AND crawl_language_code={{language}} ]]
 [[ AND transition_day_utc >= {{start_date}} ]] [[ AND transition_day_utc <= {{end_date}} ]]
GROUP BY application_name,package_name,crawl_country_code,crawl_language_code,transition_day_utc
ORDER BY application_name,transition_day_utc;
