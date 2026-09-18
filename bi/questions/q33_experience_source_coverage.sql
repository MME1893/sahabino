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
 SELECT ps.application_id,ct.country_code,ct.language_code,ps.collected_at,ps.score,
  row_number() over(partition by ps.application_id,ct.country_code,ct.language_code
                    order by ps.collected_at desc,ps.id desc) rn
 FROM public.playstore_app_snapshots ps JOIN public.crawl_tasks ct ON ct.id=ps.crawl_task_id
 WHERE ct.status='succeeded' AND ct.task_type='app_details'
), store AS (
 SELECT application_id,country_code,language_code,collected_at,score FROM store_ranked WHERE rn=1
), reviews AS (
 SELECT r.application_id,count(DISTINCT r.id)::bigint unique_sampled_reviews,
        max(ro.observed_at) latest_review_observed_at_utc
 FROM public.reviews r LEFT JOIN public.review_observations ro ON ro.review_id=r.id
 GROUP BY r.application_id
), network AS (
 SELECT em.application_package,count(DISTINCT em.capture_id)::bigint manifest_capture_count,
  count(DISTINCT nar.capture_id)::bigint analyzed_capture_count,
  count(DISTINCT nar.capture_id) FILTER(WHERE nar.comparison_ready)::bigint comparison_ready_capture_count,
  max(em.capture_finished_at_utc) latest_network_capture_at_utc
 FROM experiment_manifest em
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 GROUP BY em.application_package
)
SELECT a.name application_name,a.package_name,s.country_code store_country_code,
 s.language_code store_language_code,s.score latest_store_score_0_to_5,
 s.collected_at latest_store_observed_at_utc,r.unique_sampled_reviews,r.latest_review_observed_at_utc,
 n.manifest_capture_count,n.analyzed_capture_count,n.comparison_ready_capture_count,
 n.latest_network_capture_at_utc,
 extract(epoch from (greatest(s.collected_at,r.latest_review_observed_at_utc,n.latest_network_capture_at_utc)
  - least(s.collected_at,r.latest_review_observed_at_utc,n.latest_network_capture_at_utc)))/3600
  AS cross_source_observation_gap_hours,
 'Independent source summaries, no correlation or causal attribution'::text interpretation_limit
FROM public.applications a LEFT JOIN store s ON s.application_id=a.id
LEFT JOIN reviews r ON r.application_id=a.id LEFT JOIN network n ON n.application_package=a.package_name
WHERE a.package_name IN ('ir.android.baham','app.pinno','org.telegram.messenger','com.whatsapp')
[[AND a.package_name={{application}}]]
[[AND s.country_code={{country}}]]
[[AND s.language_code={{language}}]]
ORDER BY a.package_name,s.country_code,s.language_code;
