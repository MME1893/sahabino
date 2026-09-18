WITH release_manifest AS (
/*__RELEASE_ROWS_START__*/
SELECT NULL::uuid release_event_id,NULL::text application_package,NULL::text previous_version,
 NULL::text new_version,NULL::date release_date,NULL::text release_date_precision,
 NULL::boolean release_date_verified,NULL::date baseline_start,NULL::date baseline_end,
 NULL::date followup_start,NULL::date followup_end,NULL::date transition_period_start,
 NULL::date transition_period_end,NULL::uuid experiment_id_before,NULL::uuid experiment_id_after WHERE false
/*__RELEASE_ROWS_END__*/
), periods AS (
 SELECT rm.release_event_id,rm.application_package,rm.release_date,'before'::text period,
  rm.baseline_start period_start,rm.baseline_end period_end,
  ((rm.baseline_end+1)::timestamp AT TIME ZONE 'UTC') as_of_cutoff_exclusive_utc
 FROM release_manifest rm WHERE rm.release_date_verified AND rm.release_date_precision='day'
 UNION ALL
 SELECT rm.release_event_id,rm.application_package,rm.release_date,'after'::text,
  rm.followup_start,rm.followup_end,
  ((rm.followup_end+1)::timestamp AT TIME ZONE 'UTC')
 FROM release_manifest rm WHERE rm.release_date_verified AND rm.release_date_precision='day'
), review_candidates AS (
 SELECT p.release_event_id,p.application_package,p.release_date,p.period,p.period_start,p.period_end,
  p.as_of_cutoff_exclusive_utc,r.id review_id,r.first_observed_at,ro.score,ro.observed_at,
  ct.country_code,ct.language_code,
/*__SENTIMENT_COLUMNS_START__*/
  NULL::text AS sentiment_status,NULL::text AS sentiment_label,
  false::boolean AS sentiment_capability_available
/*__SENTIMENT_COLUMNS_END__*/
  ,row_number() over(partition by p.release_event_id,p.period,r.id
    order by ro.observed_at desc,ro.crawl_task_id desc) rn
 FROM periods p JOIN public.applications a ON a.package_name=p.application_package
 JOIN public.reviews r ON r.application_id=a.id
  AND (r.first_observed_at AT TIME ZONE 'UTC')::date BETWEEN p.period_start AND p.period_end
 JOIN public.review_observations ro ON ro.review_id=r.id
  AND ro.observed_at<p.as_of_cutoff_exclusive_utc
 JOIN public.crawl_tasks ct ON ct.id=ro.crawl_task_id
), cohort AS (
 SELECT * FROM review_candidates WHERE rn=1
), summary AS (
 SELECT release_event_id,application_package,release_date,country_code,language_code,
  max(period_end) FILTER(WHERE period='before') before_period_end,
  max(period_end) FILTER(WHERE period='after') after_period_end,
  max(as_of_cutoff_exclusive_utc) FILTER(WHERE period='before') before_as_of_cutoff_exclusive_utc,
  max(as_of_cutoff_exclusive_utc) FILTER(WHERE period='after') after_as_of_cutoff_exclusive_utc,
  count(DISTINCT review_id) FILTER(WHERE period='before') before_unique_sampled_reviews,
  count(DISTINCT review_id) FILTER(WHERE period='after') after_unique_sampled_reviews,
  avg(score::numeric) FILTER(WHERE period='before') before_mean_sampled_review_stars,
  avg(score::numeric) FILTER(WHERE period='after') after_mean_sampled_review_stars,
  count(DISTINCT review_id) FILTER(WHERE period='before' AND score<=2)::numeric /
    nullif(count(DISTINCT review_id) FILTER(WHERE period='before'),0) before_low_star_share,
  count(DISTINCT review_id) FILTER(WHERE period='after' AND score<=2)::numeric /
    nullif(count(DISTINCT review_id) FILTER(WHERE period='after'),0) after_low_star_share,
  max(observed_at) FILTER(WHERE period='before') before_latest_observation_at_utc,
  max(observed_at) FILTER(WHERE period='after') after_latest_observation_at_utc,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='before' AND sentiment_status='done'
      AND sentiment_label IN('positive','neutral','negative')) END before_classified_sentiment_reviews,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='after' AND sentiment_status='done'
      AND sentiment_label IN('positive','neutral','negative')) END after_classified_sentiment_reviews,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='before' AND NOT coalesce(
      sentiment_status='done' AND sentiment_label IN('positive','neutral','negative'),false))
    END before_historical_sentiment_unavailable_reviews,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='after' AND NOT coalesce(
      sentiment_status='done' AND sentiment_label IN('positive','neutral','negative'),false))
    END after_historical_sentiment_unavailable_reviews,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='before' AND sentiment_status='done'
      AND sentiment_label='negative')::numeric /
      nullif(count(DISTINCT review_id) FILTER(WHERE period='before' AND sentiment_status='done'
        AND sentiment_label IN('positive','neutral','negative')),0) END before_negative_sentiment_share,
  CASE WHEN bool_or(sentiment_capability_available) THEN
    count(DISTINCT review_id) FILTER(WHERE period='after' AND sentiment_status='done'
      AND sentiment_label='negative')::numeric /
      nullif(count(DISTINCT review_id) FILTER(WHERE period='after' AND sentiment_status='done'
        AND sentiment_label IN('positive','neutral','negative')),0) END after_negative_sentiment_share
 FROM cohort
 GROUP BY release_event_id,application_package,release_date,country_code,language_code
)
SELECT *,after_mean_sampled_review_stars-before_mean_sampled_review_stars AS sampled_stars_after_minus_before,
 after_low_star_share-before_low_star_share AS low_star_share_after_minus_before,
 after_negative_sentiment_share-before_negative_sentiment_share AS negative_sentiment_share_after_minus_before,
 'Each period uses the latest observation strictly before its UTC day-end cutoff, and historical sentiment is NULL when the capability or eligible historical label is unavailable'::text sentiment_limit,
 'Cohort is Sahabino first-observed date, not review publication date'::text cohort_semantics
FROM summary WHERE true
[[AND application_package={{application}}]]
[[AND country_code={{country}}]]
[[AND language_code={{language}}]]
[[AND release_event_id::text={{release_event}}]]
[[AND release_date >= {{start_date}}]]
[[AND release_date <= {{end_date}}]]
ORDER BY release_date,application_package,country_code,language_code;
