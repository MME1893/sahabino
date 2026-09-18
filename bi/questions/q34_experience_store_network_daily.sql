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
), store_ranked AS (
 SELECT a.package_name,ct.country_code,ct.language_code,ps.collected_at::date observed_day_utc,
  ps.collected_at,ps.score,row_number() over(partition by ps.application_id,ct.country_code,ct.language_code,
  ps.collected_at::date order by ps.collected_at desc,ps.id desc) rn
 FROM public.playstore_app_snapshots ps JOIN public.crawl_tasks ct ON ct.id=ps.crawl_task_id
 JOIN public.applications a ON a.id=ps.application_id
 WHERE ct.status='succeeded' AND ct.task_type='app_details'
), store_daily AS (
 SELECT package_name,country_code,language_code,observed_day_utc,collected_at,score FROM store_ranked WHERE rn=1
), network_daily AS (
 SELECT em.application_package,em.scenario,em.network_profile,em.capture_started_at_utc::date observed_day_utc,
  count(DISTINCT em.capture_id)::bigint network_capture_count,max(em.capture_finished_at_utc) network_source_at_utc,
  percentile_cont(0.5) within group(order by nar.effective_file_throughput_mbps)
   FILTER(WHERE nar.comparison_ready AND nar.effective_file_throughput_mbps IS NOT NULL)
   AS median_per_capture_effective_file_throughput_mbps,
  count(DISTINCT nar.capture_id) FILTER(WHERE nar.comparison_ready AND nar.effective_file_throughput_mbps IS NOT NULL)
   AS throughput_eligible_n
 FROM experiment_manifest em LEFT JOIN public.network_captures nc ON nc.id=em.capture_id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 GROUP BY em.application_package,em.scenario,em.network_profile,em.capture_started_at_utc::date
)
SELECT coalesce(s.package_name,n.application_package) package_name,
 coalesce(s.observed_day_utc,n.observed_day_utc) observed_day_utc,s.country_code store_country_code,
 s.language_code store_language_code,s.score store_score_0_to_5,s.collected_at store_source_at_utc,
 n.scenario,n.network_profile,n.network_capture_count,n.throughput_eligible_n,
 n.median_per_capture_effective_file_throughput_mbps,n.network_source_at_utc,
 CASE WHEN s.observed_day_utc=n.observed_day_utc THEN 'same_utc_day_only_not_causal'
      WHEN s.observed_day_utc IS NULL THEN 'store_missing_for_network_day'
      ELSE 'network_missing_for_store_day' END alignment_status
FROM store_daily s FULL OUTER JOIN network_daily n
 ON n.application_package=s.package_name AND n.observed_day_utc=s.observed_day_utc
WHERE coalesce(s.package_name,n.application_package) IN
 ('ir.android.baham','app.pinno','org.telegram.messenger','com.whatsapp')
[[AND coalesce(s.package_name,n.application_package)={{application}}]]
[[AND n.scenario={{scenario}}]]
[[AND n.network_profile={{network_profile}}]]
[[AND s.country_code={{country}}]]
[[AND s.language_code={{language}}]]
[[AND coalesce(s.observed_day_utc,n.observed_day_utc) >= {{start_date}}]]
[[AND coalesce(s.observed_day_utc,n.observed_day_utc) <= {{end_date}}]]
ORDER BY observed_day_utc,package_name,store_country_code,store_language_code,scenario,network_profile;
