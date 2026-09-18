WITH experiment_manifest AS (
/*__EXPERIMENT_ROWS_START__*/
SELECT NULL::uuid AS capture_id,NULL::uuid AS experiment_id,NULL::uuid AS session_id,NULL::integer AS trial_number,
 NULL::uuid AS pair_id,NULL::text AS application_package,NULL::text AS scenario,
 NULL::timestamptz AS capture_started_at_utc,NULL::timestamptz AS capture_finished_at_utc,
 NULL::boolean AS transfer_completed,NULL::text AS test_file_sha256,NULL::bigint AS test_file_size_bytes,
 NULL::text AS app_version,NULL::text AS device_model,NULL::text AS android_version,NULL::text AS network_type,
 NULL::text AS network_profile,NULL::text AS capture_tool,NULL::text AS capture_tool_version,
 NULL::boolean AS capture_isolation_confirmed,NULL::boolean AS cache_cleared_or_download_verified,
 NULL::text AS protocol_notes,NULL::text AS validation_notes,NULL::text AS experiment_phase WHERE false
/*__EXPERIMENT_ROWS_END__*/
), review_as_of_cohort_day AS (
 SELECT a.package_name,r.id review_id,(r.first_observed_at AT TIME ZONE 'UTC')::date cohort_day_utc,
  (((r.first_observed_at AT TIME ZONE 'UTC')::date+1)::timestamp AT TIME ZONE 'UTC') as_of_cutoff_exclusive_utc,
  row_number() over(partition by r.id order by ro.observed_at desc,ro.crawl_task_id desc) rn,ro.score
 FROM public.reviews r JOIN public.applications a ON a.id=r.application_id
 JOIN public.review_observations ro ON ro.review_id=r.id
  AND ro.observed_at < (((r.first_observed_at AT TIME ZONE 'UTC')::date+1)::timestamp AT TIME ZONE 'UTC')
), reviews_daily AS (
 SELECT package_name,cohort_day_utc,max(as_of_cutoff_exclusive_utc) as_of_cutoff_exclusive_utc,
  count(*)::bigint unique_sampled_reviews,
  avg(score::numeric) mean_sampled_review_stars_1_to_5,
  count(*) FILTER(WHERE score<=2)::numeric/nullif(count(*),0) low_star_share
 FROM review_as_of_cohort_day WHERE rn=1 GROUP BY package_name,cohort_day_utc
), network_daily AS (
 SELECT em.application_package,em.capture_started_at_utc::date capture_day_utc,em.scenario,em.network_profile,
  count(DISTINCT em.capture_id)::bigint manifest_capture_count,
  percentile_cont(0.5) within group(order by nar.effective_file_throughput_mbps)
   FILTER(WHERE nar.comparison_ready AND nar.effective_file_throughput_mbps IS NOT NULL)
   median_per_capture_effective_file_throughput_mbps
 FROM experiment_manifest em LEFT JOIN public.network_captures nc ON nc.id=em.capture_id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 GROUP BY em.application_package,em.capture_started_at_utc::date,em.scenario,em.network_profile
)
SELECT coalesce(r.package_name,n.application_package) package_name,
 coalesce(r.cohort_day_utc,n.capture_day_utc) utc_day,r.unique_sampled_reviews,
 r.as_of_cutoff_exclusive_utc,r.mean_sampled_review_stars_1_to_5,r.low_star_share,n.scenario,n.network_profile,
 n.manifest_capture_count,n.median_per_capture_effective_file_throughput_mbps,
 'Review state is latest observation strictly before the UTC day-end cutoff, so later revisions cannot rewrite the historical cohort'::text review_as_of_rule,
 'Review first-encounter cohort and network captures are independent descriptive sources, no complaint/topic inference'::text limitation
FROM reviews_daily r FULL OUTER JOIN network_daily n
 ON n.application_package=r.package_name AND n.capture_day_utc=r.cohort_day_utc
WHERE coalesce(r.package_name,n.application_package) IN
 ('ir.android.baham','app.pinno','org.telegram.messenger','com.whatsapp')
[[AND coalesce(r.package_name,n.application_package)={{application}}]]
[[AND n.scenario={{scenario}}]]
[[AND n.network_profile={{network_profile}}]]
[[AND coalesce(r.cohort_day_utc,n.capture_day_utc) >= {{start_date}}]]
[[AND coalesce(r.cohort_day_utc,n.capture_day_utc) <= {{end_date}}]]
ORDER BY utc_day,package_name,scenario,network_profile;
