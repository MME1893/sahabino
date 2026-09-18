-- Explicit investigation triggers: gaps >1 day, negative count correction or any rating change.
WITH eligible AS (
 SELECT s.id,s.application_id,a.name AS application_name,a.package_name,s.collected_at,s.score,
 s.ratings_count,s.reviews_count,ct.country_code AS crawl_country_code,ct.language_code AS crawl_language_code,
 (s.collected_at AT TIME ZONE 'UTC')::date AS snapshot_day_utc
 FROM public.playstore_app_snapshots s JOIN public.applications a ON a.id=s.application_id
 JOIN public.crawl_tasks ct ON ct.id=s.crawl_task_id AND ct.application_id=s.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='app_details' AND ct.status='succeeded'
 [[ AND a.package_name={{application}} ]] [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
), ranked AS (
 SELECT eligible.*,ROW_NUMBER() OVER(PARTITION BY application_id,crawl_country_code,crawl_language_code,snapshot_day_utc
 ORDER BY collected_at DESC,id DESC) rn FROM eligible
), daily AS (SELECT * FROM ranked WHERE rn=1), prev AS (
 SELECT daily.*,LAG(snapshot_day_utc) OVER w AS previous_day,
 LAG(score) OVER w AS previous_score,LAG(ratings_count) OVER w AS previous_ratings,
 LAG(reviews_count) OVER w AS previous_reviews,
 ROW_NUMBER() OVER(PARTITION BY application_id,crawl_country_code,crawl_language_code ORDER BY snapshot_day_utc DESC) latest_rank
 FROM daily WINDOW w AS (PARTITION BY application_id,crawl_country_code,crawl_language_code ORDER BY snapshot_day_utc)
)
SELECT application_name,package_name,crawl_country_code,crawl_language_code,snapshot_day_utc,collected_at,
 score::numeric AS latest_score_0_to_5,(score-previous_score)::numeric AS score_change_points,
 ratings_count-previous_ratings AS ratings_count_change,
 reviews_count-previous_reviews AS reviews_count_change,
 snapshot_day_utc-previous_day AS observation_gap_days,
 CASE WHEN previous_day IS NULL THEN 'no_baseline'
 WHEN ratings_count<previous_ratings OR reviews_count<previous_reviews THEN 'negative_count_correction'
 WHEN snapshot_day_utc-previous_day>1 THEN 'observation_gap'
 WHEN score<>previous_score THEN 'rating_changed'
 ELSE 'no_defined_signal' END AS investigation_signal
FROM prev WHERE latest_rank=1
ORDER BY investigation_signal,application_name;
