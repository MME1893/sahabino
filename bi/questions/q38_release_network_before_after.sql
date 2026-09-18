WITH experiment_manifest AS (
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
), release_manifest AS (
/*__RELEASE_ROWS_START__*/
SELECT NULL::uuid release_event_id,NULL::text application_package,NULL::text previous_version,
 NULL::text new_version,NULL::date release_date,NULL::text release_date_precision,
 NULL::boolean release_date_verified,NULL::date baseline_start,NULL::date baseline_end,
 NULL::date followup_start,NULL::date followup_end,NULL::date transition_period_start,
 NULL::date transition_period_end,NULL::uuid experiment_id_before,NULL::uuid experiment_id_after WHERE false
/*__RELEASE_ROWS_END__*/
), candidates AS (
 SELECT rm.release_event_id,rm.application_package,rm.release_date,em.capture_id,
  nc.id source_capture_id,CASE WHEN nc.status='analyzed' THEN nar.capture_id END analyzed_capture_id,
  em.scenario,
  em.file_cohort_id,em.test_file_size_bytes,em.device_model,em.android_version,em.network_type,
  em.network_profile,em.capture_tool,em.capture_tool_version,em.app_version,
  CASE WHEN em.experiment_id=rm.experiment_id_before AND em.capture_started_at_utc::date
       BETWEEN rm.baseline_start AND rm.baseline_end THEN 'before'
       WHEN em.experiment_id=rm.experiment_id_after AND em.capture_started_at_utc::date
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
    AND nar.tcp_present AND nar.tcp_recovery_tax IS NOT NULL) recovery_source_eligible,
  nar.effective_file_throughput_mbps,nar.total_transfer_amplification_ratio amplification,
  nar.tcp_recovery_tax,
  CASE WHEN nc.id IS NULL THEN 'capture_missing'
       WHEN nc.status<>'analyzed' THEN 'capture_not_analyzed'
       WHEN nar.capture_id IS NULL THEN 'analysis_missing'
       WHEN nc.scenario<>em.scenario OR nar.scenario<>em.scenario
         OR nar.package_name<>em.application_package THEN 'source_attribution_mismatch'
       WHEN NOT em.transfer_completed THEN 'transfer_incomplete'
       WHEN NOT em.capture_isolation_confirmed OR NOT em.cache_cleared_or_download_verified
         THEN 'provenance_incomplete'
       WHEN nc.transfer_file_size_bytes<>em.test_file_size_bytes THEN 'transfer_size_mismatch'
       WHEN NOT (CASE WHEN em.experiment_id=rm.experiment_id_before THEN em.app_version=rm.previous_version
                      WHEN em.experiment_id=rm.experiment_id_after THEN em.app_version=rm.new_version
                      ELSE false END) THEN 'application_version_mismatch'
       ELSE NULL END common_exclusion_reason
 FROM release_manifest rm JOIN experiment_manifest em ON em.application_package=rm.application_package
 LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 WHERE rm.release_date_verified AND rm.release_date_precision='day'
), period_rows AS (
 SELECT *,
  coalesce(version_matches_period AND throughput_source_eligible,false) throughput_eligible,
  coalesce(version_matches_period AND amplification_source_eligible,false) amplification_eligible,
  coalesce(version_matches_period AND recovery_source_eligible,false) recovery_eligible,
  coalesce(common_exclusion_reason,
    CASE WHEN NOT coalesce(throughput_source_eligible,false) THEN 'throughput_prerequisite_missing' END) throughput_exclusion_reason,
  coalesce(common_exclusion_reason,
    CASE WHEN NOT coalesce(amplification_source_eligible,false) THEN 'amplification_prerequisite_missing' END) amplification_exclusion_reason,
  coalesce(common_exclusion_reason,
    CASE WHEN NOT coalesce(recovery_source_eligible,false) THEN 'tcp_recovery_metric_unavailable' END) recovery_exclusion_reason
 FROM candidates WHERE period IS NOT NULL
), grouped AS (
 SELECT release_event_id,application_package,release_date,scenario,file_cohort_id,test_file_size_bytes,
  device_model,android_version,network_type,network_profile,capture_tool,capture_tool_version,
  count(DISTINCT capture_id) FILTER(WHERE period='before')::bigint before_manifest_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after')::bigint after_manifest_n,
  count(DISTINCT source_capture_id) FILTER(WHERE period='before')::bigint before_observed_n,
  count(DISTINCT source_capture_id) FILTER(WHERE period='after')::bigint after_observed_n,
  count(DISTINCT analyzed_capture_id) FILTER(WHERE period='before')::bigint before_analyzed_n,
  count(DISTINCT analyzed_capture_id) FILTER(WHERE period='after')::bigint after_analyzed_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND throughput_eligible)::bigint before_throughput_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND throughput_eligible)::bigint after_throughput_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND amplification_eligible)::bigint before_amplification_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND amplification_eligible)::bigint after_amplification_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='before' AND recovery_eligible)::bigint before_recovery_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE period='after' AND recovery_eligible)::bigint after_recovery_eligible_n,
  string_agg(DISTINCT throughput_exclusion_reason,', ') FILTER(WHERE period='before' AND NOT throughput_eligible)
    before_throughput_exclusion_reasons,
  string_agg(DISTINCT throughput_exclusion_reason,', ') FILTER(WHERE period='after' AND NOT throughput_eligible)
    after_throughput_exclusion_reasons,
  string_agg(DISTINCT amplification_exclusion_reason,', ') FILTER(WHERE period='before' AND NOT amplification_eligible)
    before_amplification_exclusion_reasons,
  string_agg(DISTINCT amplification_exclusion_reason,', ') FILTER(WHERE period='after' AND NOT amplification_eligible)
    after_amplification_exclusion_reasons,
  string_agg(DISTINCT recovery_exclusion_reason,', ') FILTER(WHERE period='before' AND NOT recovery_eligible)
    before_recovery_exclusion_reasons,
  string_agg(DISTINCT recovery_exclusion_reason,', ') FILTER(WHERE period='after' AND NOT recovery_eligible)
    after_recovery_exclusion_reasons,
  percentile_cont(0.5) within group(order by effective_file_throughput_mbps)
    FILTER(WHERE period='before' AND throughput_eligible) before_throughput_raw,
  percentile_cont(0.5) within group(order by effective_file_throughput_mbps)
    FILTER(WHERE period='after' AND throughput_eligible) after_throughput_raw,
  percentile_cont(0.5) within group(order by amplification)
    FILTER(WHERE period='before' AND amplification_eligible) before_amplification_raw,
  percentile_cont(0.5) within group(order by amplification)
    FILTER(WHERE period='after' AND amplification_eligible) after_amplification_raw,
  percentile_cont(0.5) within group(order by tcp_recovery_tax)
    FILTER(WHERE period='before' AND recovery_eligible) before_recovery_raw,
  percentile_cont(0.5) within group(order by tcp_recovery_tax)
    FILTER(WHERE period='after' AND recovery_eligible) after_recovery_raw
 FROM period_rows GROUP BY release_event_id,application_package,release_date,scenario,file_cohort_id,
  test_file_size_bytes,device_model,android_version,network_type,network_profile,capture_tool,capture_tool_version
)
SELECT *,before_throughput_eligible_n AS before_n,after_throughput_eligible_n AS after_n,
 before_recovery_eligible_n AS before_tcp_recovery_tax_eligible_n,
 after_recovery_eligible_n AS after_tcp_recovery_tax_eligible_n,
 before_manifest_n-before_throughput_eligible_n before_throughput_excluded_n,
 after_manifest_n-after_throughput_eligible_n after_throughput_excluded_n,
 before_manifest_n-before_amplification_eligible_n before_amplification_excluded_n,
 after_manifest_n-after_amplification_eligible_n after_amplification_excluded_n,
 before_manifest_n-before_recovery_eligible_n before_recovery_excluded_n,
 after_manifest_n-after_recovery_eligible_n after_recovery_excluded_n,
 CASE WHEN before_throughput_eligible_n>=3 AND after_throughput_eligible_n>=3
      THEN before_throughput_raw END before_median_effective_mbps,
 CASE WHEN before_throughput_eligible_n>=3 AND after_throughput_eligible_n>=3
      THEN after_throughput_raw END after_median_effective_mbps,
 CASE WHEN before_throughput_eligible_n>=3 AND after_throughput_eligible_n>=3
      THEN after_throughput_raw-before_throughput_raw END effective_mbps_after_minus_before,
 CASE WHEN before_amplification_eligible_n>=3 AND after_amplification_eligible_n>=3
      THEN before_amplification_raw END before_median_amplification,
 CASE WHEN before_amplification_eligible_n>=3 AND after_amplification_eligible_n>=3
      THEN after_amplification_raw END after_median_amplification,
 CASE WHEN before_amplification_eligible_n>=3 AND after_amplification_eligible_n>=3
      THEN after_amplification_raw-before_amplification_raw END amplification_after_minus_before,
 CASE WHEN before_recovery_eligible_n>=3 AND after_recovery_eligible_n>=3
      THEN before_recovery_raw END before_median_tcp_recovery_tax,
 CASE WHEN before_recovery_eligible_n>=3 AND after_recovery_eligible_n>=3
      THEN after_recovery_raw END after_median_tcp_recovery_tax,
 CASE WHEN before_recovery_eligible_n>=3 AND after_recovery_eligible_n>=3
      THEN after_recovery_raw-before_recovery_raw END tcp_recovery_tax_after_minus_before,
 CASE WHEN before_throughput_eligible_n>=3 AND after_throughput_eligible_n>=3
       AND before_amplification_eligible_n>=3 AND after_amplification_eligible_n>=3
       AND before_recovery_eligible_n>=3 AND after_recovery_eligible_n>=3
      THEN 'all_metrics_descriptive_before_after_not_causal'
      WHEN before_throughput_eligible_n>=3 AND after_throughput_eligible_n>=3
        OR before_amplification_eligible_n>=3 AND after_amplification_eligible_n>=3
        OR before_recovery_eligible_n>=3 AND after_recovery_eligible_n>=3
      THEN 'partial_metric_readiness'
      ELSE 'insufficient_under_3_each_period' END comparison_status
FROM grouped WHERE true
[[AND application_package={{application}}]]
[[AND scenario={{scenario}}]]
[[AND network_profile={{network_profile}}]]
[[AND release_event_id::text={{release_event}}]]
[[AND release_date >= {{start_date}}]]
[[AND release_date <= {{end_date}}]]
ORDER BY release_date,application_package,scenario,file_cohort_id,device_model,network_profile;
