-- First observed historical version/cohort, never mutable current review score.
WITH original AS (
 SELECT r.id AS review_id,r.first_observed_at,a.name AS application_name,a.package_name,
 ct.country_code AS crawl_country_code,ct.language_code AS crawl_language_code,
 ROW_NUMBER() OVER(PARTITION BY r.id ORDER BY o.observed_at,o.crawl_task_id) AS rn
 FROM public.review_observations o JOIN public.reviews r ON r.id=o.review_id
 JOIN public.crawl_tasks ct ON ct.id=o.crawl_task_id AND ct.application_id=r.application_id
 JOIN public.applications a ON a.id=r.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='reviews' AND ct.status='succeeded'
 [[ AND a.package_name={{application}} ]]
), firsts AS (SELECT * FROM original WHERE rn=1)
SELECT application_name,package_name,crawl_country_code,crawl_language_code,
 (first_observed_at AT TIME ZONE 'UTC')::date AS first_observed_day_utc,
 COUNT(*) AS newly_observed_unique_reviews
FROM firsts WHERE TRUE
 [[ AND crawl_country_code={{country}} ]] [[ AND crawl_language_code={{language}} ]]
 [[ AND (first_observed_at AT TIME ZONE 'UTC')::date >= {{start_date}} ]]
 [[ AND (first_observed_at AT TIME ZONE 'UTC')::date <= {{end_date}} ]]
GROUP BY application_name,package_name,crawl_country_code,crawl_language_code,(first_observed_at AT TIME ZONE 'UTC')::date
ORDER BY application_name,first_observed_day_utc;
