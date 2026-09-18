WITH runtime_capabilities AS (
/*__CAPABILITY_ROWS_START__*/
SELECT 'OFFLINE_NOT_CHECKED'::text network_state,'MANIFEST_NOT_SUPPLIED'::text experiment_state,
 'MANIFEST_NOT_SUPPLIED'::text release_state,'NOT_CHECKED'::text sentiment_state,
 'ANNOTATIONS_UNAVAILABLE'::text topic_state
/*__CAPABILITY_ROWS_END__*/
), release_manifest AS (
/*__RELEASE_ROWS_START__*/
SELECT NULL::uuid release_event_id,NULL::text application_package,NULL::text previous_version,
 NULL::text new_version,NULL::date release_date,NULL::text release_date_precision,
 NULL::boolean release_date_verified,NULL::date baseline_start,NULL::date baseline_end,
 NULL::date followup_start,NULL::date followup_end,NULL::date transition_period_start,
 NULL::date transition_period_end,NULL::uuid experiment_id_before,NULL::uuid experiment_id_after WHERE false
/*__RELEASE_ROWS_END__*/
), experiment_manifest AS (
/*__EXPERIMENT_ROWS_START__*/
SELECT NULL::uuid capture_id,NULL::uuid experiment_id,NULL::uuid session_id,NULL::integer trial_number,
 NULL::uuid pair_id,NULL::text comparison_cohort_id,NULL::text file_cohort_id,
 NULL::text application_package,NULL::text scenario,NULL::timestamptz capture_started_at_utc,
 NULL::timestamptz capture_finished_at_utc,NULL::boolean transfer_completed,NULL::bigint test_file_size_bytes,
 NULL::text app_version,NULL::text device_model,NULL::text android_version,NULL::text network_type,
 NULL::text network_profile,NULL::text capture_tool,NULL::text capture_tool_version,
 NULL::boolean capture_isolation_confirmed,NULL::boolean cache_cleared_or_download_verified,
 NULL::text experiment_phase WHERE false
/*__EXPERIMENT_ROWS_END__*/
), store AS (
 SELECT rm.release_event_id,ct.country_code,ct.language_code,
  count(DISTINCT (ps.collected_at AT TIME ZONE 'UTC')::date) FILTER(
    WHERE (ps.collected_at AT TIME ZONE 'UTC')::date
    BETWEEN rm.baseline_start AND rm.baseline_end) store_before_days,
  count(DISTINCT (ps.collected_at AT TIME ZONE 'UTC')::date) FILTER(
    WHERE (ps.collected_at AT TIME ZONE 'UTC')::date
    BETWEEN rm.followup_start AND rm.followup_end) store_after_days,
  max(ps.collected_at) store_latest_source_at
 FROM release_manifest rm
 JOIN public.applications a ON a.package_name=rm.application_package
 JOIN public.playstore_app_snapshots ps ON ps.application_id=a.id
 JOIN public.crawl_tasks ct ON ct.id=ps.crawl_task_id
  AND ct.application_id=ps.application_id AND ct.task_type='app_details' AND ct.status='succeeded'
 WHERE (ps.collected_at AT TIME ZONE 'UTC')::date BETWEEN rm.baseline_start AND rm.baseline_end
    OR (ps.collected_at AT TIME ZONE 'UTC')::date BETWEEN rm.followup_start AND rm.followup_end
 GROUP BY rm.release_event_id,ct.country_code,ct.language_code
), review_periods AS (
 SELECT rm.release_event_id,rm.application_package,'before'::text period,
  rm.baseline_start period_start,rm.baseline_end period_end,
  ((rm.baseline_end+1)::timestamp AT TIME ZONE 'UTC') as_of_cutoff_exclusive_utc
 FROM release_manifest rm
 UNION ALL
 SELECT rm.release_event_id,rm.application_package,'after'::text,
  rm.followup_start,rm.followup_end,
  ((rm.followup_end+1)::timestamp AT TIME ZONE 'UTC')
 FROM release_manifest rm
), review_candidates AS (
 SELECT p.release_event_id,p.period,r.id review_id,ro.observed_at,
  ct.country_code,ct.language_code,
  row_number() over(partition by p.release_event_id,p.period,r.id
    order by ro.observed_at desc,ro.crawl_task_id desc) rn
 FROM review_periods p
 JOIN public.applications a ON a.package_name=p.application_package
 JOIN public.reviews r ON r.application_id=a.id
  AND (r.first_observed_at AT TIME ZONE 'UTC')::date BETWEEN p.period_start AND p.period_end
 JOIN public.review_observations ro ON ro.review_id=r.id
  AND ro.observed_at<p.as_of_cutoff_exclusive_utc
 JOIN public.crawl_tasks ct ON ct.id=ro.crawl_task_id
  AND ct.application_id=a.id AND ct.task_type='reviews' AND ct.status='succeeded'
), reviews AS (
 SELECT release_event_id,country_code,language_code,
  count(DISTINCT review_id) FILTER(WHERE period='before') review_before_n,
  count(DISTINCT review_id) FILTER(WHERE period='after') review_after_n,
  max(observed_at) review_latest_source_at
 FROM review_candidates WHERE rn=1
 GROUP BY release_event_id,country_code,language_code
),
/*__OPTIONAL_NETWORK_START__*/
network_candidates AS (
 SELECT rm.release_event_id,rm.application_package,em.capture_id,nc.id source_capture_id,
  CASE WHEN nc.status='analyzed' THEN nar.capture_id END analyzed_capture_id,
  em.scenario,em.file_cohort_id,
  em.test_file_size_bytes,em.device_model,em.android_version,em.network_type,em.network_profile,
  em.capture_tool,em.capture_tool_version,em.capture_finished_at_utc,
  CASE WHEN em.experiment_id=rm.experiment_id_before
       AND (em.capture_started_at_utc AT TIME ZONE 'UTC')::date
       BETWEEN rm.baseline_start AND rm.baseline_end THEN 'before'
       WHEN em.experiment_id=rm.experiment_id_after
       AND (em.capture_started_at_utc AT TIME ZONE 'UTC')::date
       BETWEEN rm.followup_start AND rm.followup_end THEN 'after' END period,
  CASE WHEN em.experiment_id=rm.experiment_id_before THEN em.app_version=rm.previous_version
       WHEN em.experiment_id=rm.experiment_id_after THEN em.app_version=rm.new_version
       ELSE false END version_matches_period,
  (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
   AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
   AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
   AND nar.comparison_ready AND nar.direction_metadata_available AND nar.truncated_packet_count=0
   AND nar.observed_primary_payload_span_ms>0 AND nar.effective_file_throughput_mbps IS NOT NULL)
    throughput_source_eligible,
  (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
   AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
   AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
   AND nar.total_transfer_amplification_ratio IS NOT NULL) amplification_source_eligible,
  (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
   AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
   AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
   AND nar.tcp_present AND nar.tcp_recovery_tax IS NOT NULL) recovery_source_eligible
 FROM release_manifest rm JOIN experiment_manifest em ON em.application_package=rm.application_package
 LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 WHERE rm.release_date_verified AND rm.release_date_precision='day'
), network AS (
 SELECT release_event_id,application_package,scenario,file_cohort_id,test_file_size_bytes,device_model,
  android_version,network_type,network_profile,capture_tool,capture_tool_version,
  count(DISTINCT capture_id) FILTER(WHERE period='before')::bigint network_before_manifest_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after')::bigint network_after_manifest_n,
  count(DISTINCT source_capture_id) FILTER(WHERE period='before')::bigint network_before_observed_n,
  count(DISTINCT source_capture_id) FILTER(WHERE period='after')::bigint network_after_observed_n,
  count(DISTINCT analyzed_capture_id) FILTER(WHERE period='before')::bigint network_before_analyzed_n,
  count(DISTINCT analyzed_capture_id) FILTER(WHERE period='after')::bigint network_after_analyzed_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND version_matches_period
    AND coalesce(throughput_source_eligible,false))::bigint before_throughput_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND version_matches_period
    AND coalesce(throughput_source_eligible,false))::bigint after_throughput_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND version_matches_period
    AND coalesce(amplification_source_eligible,false))::bigint before_amplification_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND version_matches_period
    AND coalesce(amplification_source_eligible,false))::bigint after_amplification_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND version_matches_period
    AND coalesce(recovery_source_eligible,false))::bigint before_recovery_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND version_matches_period
    AND coalesce(recovery_source_eligible,false))::bigint after_recovery_eligible_n,
  max(capture_finished_at_utc) network_latest_source_at
 FROM network_candidates WHERE period IS NOT NULL
 GROUP BY release_event_id,application_package,scenario,file_cohort_id,test_file_size_bytes,device_model,
  android_version,network_type,network_profile,capture_tool,capture_tool_version
)
/*__OPTIONAL_NETWORK_END__*/
SELECT rm.release_event_id,rm.application_package,rm.previous_version,rm.new_version,rm.release_date,
 rm.release_date_precision,rm.release_date_verified,
 s.country_code AS store_country_code,s.language_code AS store_language_code,
 n.scenario,n.file_cohort_id,n.test_file_size_bytes,
 n.device_model,n.android_version,n.network_type,n.network_profile,n.capture_tool,n.capture_tool_version,
 s.store_before_days,s.store_after_days,r.review_before_n,r.review_after_n,
 n.network_before_manifest_n,n.network_after_manifest_n,
 n.network_before_observed_n,n.network_after_observed_n,
 n.network_before_analyzed_n,n.network_after_analyzed_n,
 n.before_throughput_eligible_n AS network_before_n,n.after_throughput_eligible_n AS network_after_n,
 n.before_throughput_eligible_n,n.after_throughput_eligible_n,
 n.before_amplification_eligible_n,n.after_amplification_eligible_n,
 n.before_recovery_eligible_n AS before_tcp_recovery_tax_eligible_n,
 n.after_recovery_eligible_n AS after_tcp_recovery_tax_eligible_n,
 s.store_latest_source_at,r.review_latest_source_at,n.network_latest_source_at,c.network_state,
 CASE WHEN s.release_event_id IS NULL THEN 'store_locale_unavailable'
      WHEN s.store_before_days=0 OR s.store_after_days=0 THEN 'store_same_locale_period_missing'
      ELSE 'store_same_locale_ready' END store_locale_status,
 CASE WHEN r.release_event_id IS NULL OR r.review_before_n=0 OR r.review_after_n=0
      THEN 'same_locale_review_cohort_missing'
      ELSE 'same_locale_review_cohort_ready' END store_review_locale_status,
 CASE WHEN NOT rm.release_date_verified OR rm.release_date_precision<>'day' THEN 'release_metadata_insufficient'
      WHEN rm.baseline_start IS NULL OR rm.baseline_end IS NULL OR rm.followup_start IS NULL OR rm.followup_end IS NULL
       THEN 'release_windows_missing'
      WHEN s.release_event_id IS NULL OR s.store_before_days=0 OR s.store_after_days=0
       THEN 'store_same_locale_period_missing'
      WHEN r.release_event_id IS NULL OR r.review_before_n=0 OR r.review_after_n=0
       THEN 'same_locale_review_cohort_missing'
      WHEN c.network_state NOT IN('NETWORK_DATA_AVAILABLE','NETWORK_COMPARISON_INSUFFICIENT','NETWORK_COMPARISON_READY')
       THEN 'base_store_review_ready_network_unavailable'
      WHEN n.release_event_id IS NULL THEN 'no_period_eligible_network_conditions'
      WHEN n.before_throughput_eligible_n<3 OR n.after_throughput_eligible_n<3
        OR n.before_amplification_eligible_n<3 OR n.after_amplification_eligible_n<3
        OR n.before_recovery_eligible_n<3 OR n.after_recovery_eligible_n<3
       THEN 'network_metric_period_insufficient'
      ELSE 'multi_source_descriptive_evidence_available' END evidence_matrix_status,
 'Store and Review evidence share the displayed historical crawl locale, while Network is optional and remains observational at matched experimental conditions'::text limitation
FROM release_manifest rm
LEFT JOIN store s USING(release_event_id)
LEFT JOIN reviews r ON r.release_event_id=rm.release_event_id
 AND r.country_code=s.country_code AND r.language_code=s.language_code
LEFT JOIN network n ON n.release_event_id=rm.release_event_id
CROSS JOIN runtime_capabilities c
WHERE true
[[AND rm.application_package={{application}}]]
[[AND rm.release_event_id::text={{release_event}}]]
[[AND rm.release_date >= {{start_date}}]]
[[AND rm.release_date <= {{end_date}}]]
ORDER BY rm.release_date,rm.application_package,s.country_code,s.language_code,
 n.scenario,n.file_cohort_id,n.device_model,n.network_profile;
