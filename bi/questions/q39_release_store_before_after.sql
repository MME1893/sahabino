WITH release_manifest AS (
/*__RELEASE_ROWS_START__*/
SELECT NULL::uuid release_event_id,NULL::text application_package,NULL::text previous_version,
 NULL::text new_version,NULL::date release_date,NULL::text release_date_precision,
 NULL::text release_date_source,NULL::text release_evidence_reference,NULL::boolean release_date_verified,
 NULL::date baseline_start,NULL::date baseline_end,NULL::date followup_start,NULL::date followup_end,
 NULL::date transition_period_start,NULL::date transition_period_end,NULL::uuid experiment_id_before,
 NULL::uuid experiment_id_after,NULL::text notes WHERE false
/*__RELEASE_ROWS_END__*/
), daily_ranked AS (
 SELECT a.package_name,ct.country_code,ct.language_code,ps.collected_at::date day_utc,ps.collected_at,
  ps.score,ps.ratings_count,ps.reviews_count,ps.version,
  row_number() over(partition by ps.application_id,ct.country_code,ct.language_code,ps.collected_at::date
                    order by ps.collected_at desc,ps.id desc) rn
 FROM public.playstore_app_snapshots ps JOIN public.crawl_tasks ct ON ct.id=ps.crawl_task_id
 JOIN public.applications a ON a.id=ps.application_id
 WHERE ct.status='succeeded' AND ct.task_type='app_details'
), daily AS (SELECT * FROM daily_ranked WHERE rn=1), periods AS (
 SELECT rm.release_event_id,rm.application_package,rm.release_date,d.country_code,d.language_code,d.day_utc,
  d.collected_at,d.score,d.ratings_count,d.reviews_count,d.version,
  CASE WHEN d.day_utc BETWEEN rm.baseline_start AND rm.baseline_end THEN 'before'
       WHEN d.day_utc BETWEEN rm.followup_start AND rm.followup_end THEN 'after' END period
 FROM release_manifest rm JOIN daily d ON d.package_name=rm.application_package
 WHERE rm.release_date_verified AND rm.release_date_precision='day'
), endpoints AS (
 SELECT *,row_number() over(partition by release_event_id,country_code,language_code,period
  order by day_utc desc,collected_at desc) endpoint_rank FROM periods WHERE period IS NOT NULL
), summary AS (
 SELECT release_event_id,application_package,release_date,country_code,language_code,
  count(DISTINCT day_utc) FILTER(WHERE period='before') before_observed_days,
  count(DISTINCT day_utc) FILTER(WHERE period='after') after_observed_days,
  percentile_cont(0.5) within group(order by score) FILTER(WHERE period='before') before_median_daily_store_score,
  percentile_cont(0.5) within group(order by score) FILTER(WHERE period='after') after_median_daily_store_score,
  max(ratings_count) FILTER(WHERE period='before' AND endpoint_rank=1) before_endpoint_ratings_count,
  max(ratings_count) FILTER(WHERE period='after' AND endpoint_rank=1) after_endpoint_ratings_count,
  max(reviews_count) FILTER(WHERE period='before' AND endpoint_rank=1) before_endpoint_reviews_count,
  max(reviews_count) FILTER(WHERE period='after' AND endpoint_rank=1) after_endpoint_reviews_count,
  max(collected_at) FILTER(WHERE period='before') before_latest_source_at_utc,
  max(collected_at) FILTER(WHERE period='after') after_latest_source_at_utc
 FROM endpoints GROUP BY release_event_id,application_package,release_date,country_code,language_code
)
SELECT *,after_median_daily_store_score-before_median_daily_store_score AS store_score_after_minus_before,
 after_endpoint_ratings_count-before_endpoint_ratings_count AS observed_ratings_count_endpoint_difference,
 after_endpoint_reviews_count-before_endpoint_reviews_count AS observed_reviews_count_endpoint_difference,
 CASE WHEN before_observed_days=0 OR after_observed_days=0 THEN 'missing_period'
      ELSE 'observational_store_before_after_historical_locale' END comparison_status
FROM summary WHERE true
[[AND application_package={{application}}]]
[[AND country_code={{country}}]]
[[AND language_code={{language}}]]
[[AND release_event_id::text={{release_event}}]]
[[AND release_date >= {{start_date}}]]
[[AND release_date <= {{end_date}}]]
ORDER BY release_date,application_package,country_code,language_code;
