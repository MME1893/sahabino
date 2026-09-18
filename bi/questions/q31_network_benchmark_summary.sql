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
), trials AS (
 SELECT em.*,a.name application_name,(nar.capture_id IS NOT NULL) analyzed,
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
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND nar.network_bytes_total>0
    AND nar.ip_transport_header_overhead_ratio IS NOT NULL) overhead_eligible,
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND nar.tcp_present AND nar.tcp_rtt_available
    AND nar.tcp_initial_rtt_avg_ms IS NOT NULL) initial_rtt_eligible,
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND nar.tcp_present AND nar.tcp_rtt_available
    AND nar.tcp_ack_rtt_p95_ms IS NOT NULL) ack_rtt_eligible,
   (nc.status='analyzed' AND nc.scenario=em.scenario AND nar.scenario=em.scenario
    AND nar.package_name=em.application_package AND nar.tcp_present
    AND nar.tcp_recovery_tax IS NOT NULL) recovery_tax_eligible,
   nar.effective_file_throughput_mbps,nar.total_transfer_amplification_ratio,
   nar.ip_transport_header_overhead_ratio,nar.tcp_initial_rtt_avg_ms,
   nar.tcp_ack_rtt_p95_ms,nar.tcp_recovery_tax
 FROM experiment_manifest em
 LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
 WHERE true
 [[AND em.application_package={{application}}]]
 [[AND em.scenario={{scenario}}]]
 [[AND em.experiment_id::text={{experiment}}]]
 [[AND em.session_id::text={{session}}]]
 [[AND em.network_profile={{network_profile}}]]
 [[AND em.capture_started_at_utc::date >= {{start_date}}]]
 [[AND em.capture_started_at_utc::date <= {{end_date}}]]
), grouped AS (
 SELECT experiment_id,session_id,application_name,application_package,scenario,comparison_cohort_id,
  file_cohort_id,test_file_size_bytes,app_version,device_model,android_version,network_type,
  network_profile,capture_tool,capture_tool_version,experiment_phase,
  count(DISTINCT capture_id)::bigint n_total,
  count(DISTINCT capture_id) FILTER(WHERE analyzed)::bigint n_analyzed,
  count(DISTINCT capture_id) FILTER(WHERE throughput_eligible)::bigint throughput_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE amplification_eligible)::bigint amplification_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE overhead_eligible)::bigint overhead_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE initial_rtt_eligible)::bigint initial_rtt_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE ack_rtt_eligible)::bigint ack_rtt_eligible_n,
  count(DISTINCT capture_id) FILTER(WHERE recovery_tax_eligible)::bigint recovery_tax_eligible_n,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY effective_file_throughput_mbps)
    FILTER(WHERE throughput_eligible) throughput_median_raw,
  min(effective_file_throughput_mbps) FILTER(WHERE throughput_eligible) throughput_min_raw,
  max(effective_file_throughput_mbps) FILTER(WHERE throughput_eligible) throughput_max_raw,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY total_transfer_amplification_ratio)
    FILTER(WHERE amplification_eligible) amplification_median_raw,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY ip_transport_header_overhead_ratio)
    FILTER(WHERE overhead_eligible) overhead_median_raw,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY tcp_initial_rtt_avg_ms)
    FILTER(WHERE initial_rtt_eligible) initial_rtt_median_raw,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY tcp_ack_rtt_p95_ms)
    FILTER(WHERE ack_rtt_eligible) ack_rtt_median_raw,
  percentile_cont(0.5) WITHIN GROUP(ORDER BY tcp_recovery_tax)
    FILTER(WHERE recovery_tax_eligible) recovery_tax_median_raw,
  array_agg(effective_file_throughput_mbps ORDER BY trial_number,capture_id)
    FILTER(WHERE throughput_eligible) throughput_values_raw
 FROM trials
 GROUP BY experiment_id,session_id,application_name,application_package,scenario,comparison_cohort_id,
  file_cohort_id,test_file_size_bytes,app_version,device_model,android_version,network_type,
  network_profile,capture_tool,capture_tool_version,experiment_phase
)
SELECT experiment_id,session_id,application_name,application_package,scenario,comparison_cohort_id,
 file_cohort_id,test_file_size_bytes,app_version,device_model,android_version,network_type,
 network_profile,capture_tool,capture_tool_version,experiment_phase,n_total,n_analyzed,
 throughput_eligible_n AS n_valid,throughput_eligible_n,amplification_eligible_n,overhead_eligible_n,
 initial_rtt_eligible_n,ack_rtt_eligible_n,recovery_tax_eligible_n,
 CASE WHEN throughput_eligible_n<3 THEN 'insufficient_under_3' WHEN throughput_eligible_n<=5
      THEN 'descriptive_only_3_to_5' ELSE 'descriptive_sample_over_5' END comparison_status,
 CASE WHEN throughput_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END throughput_status,
 CASE WHEN amplification_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END amplification_status,
 CASE WHEN overhead_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END overhead_status,
 CASE WHEN initial_rtt_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END initial_rtt_status,
 CASE WHEN ack_rtt_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END ack_rtt_status,
 CASE WHEN recovery_tax_eligible_n<3 THEN 'insufficient_under_3' ELSE 'eligible' END recovery_tax_status,
 CASE WHEN throughput_eligible_n>=3 THEN throughput_median_raw END median_per_capture_effective_file_throughput_mbps,
 CASE WHEN throughput_eligible_n>=3 THEN throughput_min_raw END min_effective_file_throughput_mbps,
 CASE WHEN throughput_eligible_n>=3 THEN throughput_max_raw END max_effective_file_throughput_mbps,
 CASE WHEN amplification_eligible_n>=3 THEN amplification_median_raw END median_per_capture_total_transfer_amplification_ratio,
 CASE WHEN overhead_eligible_n>=3 THEN overhead_median_raw END median_per_capture_ip_transport_header_overhead_ratio,
 CASE WHEN initial_rtt_eligible_n>=3 THEN initial_rtt_median_raw END median_of_per_capture_tcp_initial_rtt_average_ms,
 CASE WHEN ack_rtt_eligible_n>=3 THEN ack_rtt_median_raw END median_of_per_capture_tcp_ack_rtt_p95_ms,
 CASE WHEN recovery_tax_eligible_n>=3 THEN recovery_tax_median_raw END median_per_capture_tcp_recovery_tax,
 CASE WHEN throughput_eligible_n>=3 THEN throughput_values_raw END eligible_trial_effective_throughput_values_mbps
FROM grouped
ORDER BY experiment_id,session_id,application_package,scenario,file_cohort_id,device_model,network_profile;
