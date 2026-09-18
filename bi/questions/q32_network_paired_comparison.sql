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
), eligible AS (
 SELECT em.*,
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
    AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
    AND nar.comparison_ready AND nar.direction_metadata_available AND nar.truncated_packet_count=0
    AND nar.observed_primary_payload_span_ms>0 AND nar.effective_file_throughput_mbps IS NOT NULL)
      throughput_eligible,
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND em.transfer_completed AND em.capture_isolation_confirmed
    AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
    AND nar.total_transfer_amplification_ratio IS NOT NULL) amplification_eligible,
   nar.effective_file_throughput_mbps,nar.total_transfer_amplification_ratio
 FROM experiment_manifest em
 LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 WHERE em.pair_id IS NOT NULL
), numbered AS (
 SELECT e.*,row_number() over(partition by pair_id order by application_package,capture_id) side
 FROM eligible e
), pairs AS (
 SELECT pair_id,min(experiment_id::text)::uuid experiment_id,min(session_id::text)::uuid session_id,
  min(comparison_cohort_id) comparison_cohort_id,min(scenario) scenario,min(file_cohort_id) file_cohort_id,
  min(test_file_size_bytes) test_file_size_bytes,min(device_model) device_model,
  min(android_version) android_version,min(network_type) network_type,min(network_profile) network_profile,
  min(capture_tool) capture_tool,min(capture_tool_version) capture_tool_version,
  max(capture_started_at_utc) pair_observed_at_utc,
  count(*) pair_members,count(DISTINCT application_package) distinct_applications,
  count(DISTINCT experiment_id) experiment_count,count(DISTINCT session_id) session_count,
  count(DISTINCT scenario) scenario_count,count(DISTINCT file_cohort_id) file_count,
  count(DISTINCT trial_number) trial_number_count,
  count(DISTINCT test_file_size_bytes) file_size_count,count(DISTINCT device_model) device_count,
  count(DISTINCT android_version) android_version_count,count(DISTINCT network_type) network_type_count,
  count(DISTINCT network_profile) network_profile_count,count(DISTINCT comparison_cohort_id) cohort_count,
  count(DISTINCT capture_tool) capture_tool_count,
  count(DISTINCT capture_tool_version) capture_tool_version_count,
  count(DISTINCT experiment_phase) experiment_phase_count,
  max(application_package) FILTER(WHERE side=1) application_a,
  max(application_package) FILTER(WHERE side=2) application_b,
  bool_and(throughput_eligible) throughput_both_eligible,
  bool_and(amplification_eligible) amplification_both_eligible,
  bool_or(throughput_eligible) FILTER(WHERE side=1) application_a_throughput_eligible,
  bool_or(throughput_eligible) FILTER(WHERE side=2) application_b_throughput_eligible,
  bool_or(amplification_eligible) FILTER(WHERE side=1) application_a_amplification_eligible,
  bool_or(amplification_eligible) FILTER(WHERE side=2) application_b_amplification_eligible,
  max(effective_file_throughput_mbps) FILTER(WHERE side=1 AND throughput_eligible) application_a_effective_mbps,
  max(effective_file_throughput_mbps) FILTER(WHERE side=2 AND throughput_eligible) application_b_effective_mbps,
  max(total_transfer_amplification_ratio) FILTER(WHERE side=1 AND amplification_eligible) application_a_amplification,
  max(total_transfer_amplification_ratio) FILTER(WHERE side=2 AND amplification_eligible) application_b_amplification
 FROM numbered GROUP BY pair_id
), validated AS (
 SELECT *,(
   pair_members=2 AND distinct_applications=2 AND experiment_count=1 AND session_count=1
   AND scenario_count=1 AND trial_number_count=1 AND file_count=1 AND file_size_count=1 AND device_count=1
   AND android_version_count=1 AND network_type_count=1 AND network_profile_count=1
   AND cohort_count=1 AND capture_tool_count=1 AND capture_tool_version_count=1
   AND experiment_phase_count=1 AND comparison_cohort_id IS NOT NULL
 ) pair_conditions_valid
 FROM pairs
)
SELECT pair_id,experiment_id,session_id,comparison_cohort_id,scenario,file_cohort_id,test_file_size_bytes,
 device_model,android_version,network_type,network_profile,capture_tool,capture_tool_version,
 pair_observed_at_utc,application_a,application_b,pair_members,pair_conditions_valid,
 application_a_throughput_eligible,application_b_throughput_eligible,throughput_both_eligible,
 application_a_amplification_eligible,application_b_amplification_eligible,amplification_both_eligible,
 application_a_effective_mbps,application_b_effective_mbps,
 application_a_amplification,application_b_amplification,
 CASE WHEN pair_conditions_valid AND throughput_both_eligible
      THEN application_b_effective_mbps-application_a_effective_mbps END b_minus_a_effective_mbps,
 CASE WHEN pair_conditions_valid AND amplification_both_eligible
      THEN application_b_amplification-application_a_amplification END b_minus_a_amplification,
 CASE WHEN NOT pair_conditions_valid THEN 'invalid_pair_identity_or_conditions'
      WHEN NOT throughput_both_eligible AND NOT amplification_both_eligible THEN 'both_metrics_ineligible'
      WHEN NOT throughput_both_eligible THEN 'throughput_pair_ineligible'
      WHEN NOT amplification_both_eligible THEN 'amplification_pair_ineligible'
      ELSE 'descriptive_paired_observation' END pair_status
FROM validated
WHERE true
[[AND (application_a={{application}} OR application_b={{application}})]]
[[AND scenario={{scenario}}]]
[[AND experiment_id::text={{experiment}}]]
[[AND session_id::text={{session}}]]
[[AND network_profile={{network_profile}}]]
[[AND pair_observed_at_utc::date >= {{start_date}}]]
[[AND pair_observed_at_utc::date <= {{end_date}}]]
ORDER BY experiment_id,session_id,pair_id;
