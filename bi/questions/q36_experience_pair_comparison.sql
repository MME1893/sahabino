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
), app_dimension AS (
 SELECT a.id,a.name,a.package_name,CASE WHEN a.package_name IN('ir.android.baham','app.pinno') THEN 'Baham vs Pinno'
  WHEN a.package_name IN('org.telegram.messenger','com.whatsapp') THEN 'Telegram vs WhatsApp' END comparison_pair
 FROM public.applications a WHERE a.package_name IN
 ('ir.android.baham','app.pinno','org.telegram.messenger','com.whatsapp')
), store AS (
 SELECT DISTINCT ON(ps.application_id,ct.country_code,ct.language_code)
  ps.application_id,ps.score,ps.collected_at,ct.country_code,ct.language_code
 FROM public.playstore_app_snapshots ps JOIN public.crawl_tasks ct ON ct.id=ps.crawl_task_id
 WHERE ct.status='succeeded'
 ORDER BY ps.application_id,ct.country_code,ct.language_code,ps.collected_at desc,ps.id desc
), review_ranked AS (
 SELECT r.application_id,r.id review_id,ro.score,
  row_number() over(partition by r.id order by ro.observed_at desc,ro.crawl_task_id desc) rn
 FROM public.reviews r JOIN public.review_observations ro ON ro.review_id=r.id
), review AS (
 SELECT application_id,count(*)::bigint unique_sampled_reviews,
  avg(score::numeric) mean_observed_review_stars
 FROM review_ranked WHERE rn=1 GROUP BY application_id
), network AS (
 SELECT em.application_package,em.experiment_id,em.session_id,em.scenario,em.file_cohort_id,
  em.test_file_size_bytes,em.app_version,em.device_model,em.android_version,em.network_type,
  em.network_profile,em.capture_tool,em.capture_tool_version,em.experiment_phase,
  count(DISTINCT em.capture_id)::bigint manifest_capture_count,
  count(DISTINCT em.capture_id) FILTER(WHERE nc.status='analyzed' AND nc.scenario=em.scenario
   AND nar.scenario=em.scenario AND nar.package_name=em.application_package AND em.transfer_completed
   AND em.capture_isolation_confirmed AND em.cache_cleared_or_download_verified
   AND nc.transfer_file_size_bytes=em.test_file_size_bytes AND nar.comparison_ready
   AND nar.direction_metadata_available AND nar.truncated_packet_count=0
   AND nar.observed_primary_payload_span_ms>0 AND nar.effective_file_throughput_mbps IS NOT NULL
  )::bigint comparison_ready_n,
  percentile_cont(0.5) within group(order by nar.effective_file_throughput_mbps)
   FILTER(WHERE nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
    AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
    AND nar.comparison_ready AND nar.direction_metadata_available AND nar.truncated_packet_count=0
    AND nar.observed_primary_payload_span_ms>0 AND nar.effective_file_throughput_mbps IS NOT NULL)
   median_raw,max(em.capture_finished_at_utc) network_source_at_utc
 FROM experiment_manifest em LEFT JOIN public.network_captures nc ON nc.id=em.capture_id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 GROUP BY em.application_package,em.experiment_id,em.session_id,em.scenario,em.file_cohort_id,
  em.test_file_size_bytes,em.app_version,em.device_model,em.android_version,em.network_type,
  em.network_profile,em.capture_tool,em.capture_tool_version,em.experiment_phase
)
SELECT d.comparison_pair,d.name application_name,d.package_name,s.country_code store_country_code,
 s.language_code store_language_code,s.score latest_store_score_0_to_5,s.collected_at store_source_at_utc,
 r.unique_sampled_reviews,r.mean_observed_review_stars,n.experiment_id,n.session_id,n.scenario,
 n.file_cohort_id,n.test_file_size_bytes,n.app_version,n.device_model,n.android_version,n.network_type,
 n.network_profile,n.capture_tool,n.capture_tool_version,n.experiment_phase,
 n.manifest_capture_count,n.comparison_ready_n,
 CASE WHEN n.comparison_ready_n>=3 THEN n.median_raw END median_per_capture_effective_file_throughput_mbps,
 n.network_source_at_utc,
 CASE WHEN coalesce(n.comparison_ready_n,0)<3 THEN 'network_comparison_insufficient'
      WHEN n.comparison_ready_n<=5 THEN 'network_descriptive_only_3_to_5'
      ELSE 'network_descriptive_sample_over_5' END network_interpretation,
 'Side-by-side source measures, no composite score, ranking, superiority, correlation, or causality'::text limitation
FROM app_dimension d LEFT JOIN store s ON s.application_id=d.id LEFT JOIN review r ON r.application_id=d.id
LEFT JOIN network n ON n.application_package=d.package_name
WHERE true
[[AND d.package_name={{application}}]]
[[AND d.comparison_pair={{comparison_pair}}]]
[[AND s.country_code={{country}}]]
[[AND s.language_code={{language}}]]
ORDER BY d.comparison_pair,d.package_name;
