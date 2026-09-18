-- Review crawler task coverage vs observed DISTINCT review sample, not total-store representativeness.
WITH tasks AS (
 SELECT a.id AS application_id,a.name AS application_name,a.package_name,
 ct.country_code AS crawl_country_code,ct.language_code AS crawl_language_code,
 COUNT(*) AS review_crawl_tasks,COUNT(*) FILTER(WHERE ct.status='succeeded') AS successful_review_crawl_tasks
 FROM public.crawl_tasks ct JOIN public.applications a ON a.id=ct.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='reviews'
 [[ AND a.package_name={{application}} ]] [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
 GROUP BY a.id,a.name,a.package_name,ct.country_code,ct.language_code
), sample AS (
 SELECT ct.application_id,ct.country_code AS crawl_country_code,ct.language_code AS crawl_language_code,
 COUNT(DISTINCT o.review_id) AS unique_reviews_in_sample,MAX(o.observed_at) AS latest_review_observation_at
 FROM public.review_observations o JOIN public.crawl_tasks ct ON ct.id=o.crawl_task_id
 JOIN public.reviews r ON r.id=o.review_id AND r.application_id=ct.application_id
 JOIN public.applications a ON a.id=r.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='reviews' AND ct.status='succeeded'
 [[ AND a.package_name={{application}} ]] [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
 GROUP BY ct.application_id,ct.country_code,ct.language_code
)
SELECT t.application_name,t.package_name,t.crawl_country_code,t.crawl_language_code,
 t.review_crawl_tasks,t.successful_review_crawl_tasks,
 t.successful_review_crawl_tasks::numeric/NULLIF(t.review_crawl_tasks,0) AS crawl_task_success_share,
 COALESCE(s.unique_reviews_in_sample,0) AS unique_reviews_in_sample,
 s.latest_review_observation_at
FROM tasks t LEFT JOIN sample s ON s.application_id=t.application_id
 AND s.crawl_country_code=t.crawl_country_code AND s.crawl_language_code=t.crawl_language_code
ORDER BY t.application_name,t.crawl_country_code,t.crawl_language_code;
