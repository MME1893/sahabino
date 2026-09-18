WITH experiment_manifest AS (
/*__EXPERIMENT_ROWS_START__*/
SELECT NULL::uuid AS capture_id, NULL::uuid AS experiment_id, NULL::uuid AS session_id,
 NULL::integer AS trial_number, NULL::uuid AS pair_id, NULL::text AS application_package,
 NULL::text AS scenario, NULL::timestamptz AS capture_started_at_utc,
 NULL::timestamptz AS capture_finished_at_utc, NULL::boolean AS transfer_completed,
 NULL::text AS test_file_sha256, NULL::bigint AS test_file_size_bytes, NULL::text AS app_version,
 NULL::text AS device_model, NULL::text AS android_version, NULL::text AS network_type,
 NULL::text AS network_profile, NULL::text AS capture_tool, NULL::text AS capture_tool_version,
 NULL::boolean AS capture_isolation_confirmed,
 NULL::boolean AS cache_cleared_or_download_verified, NULL::text AS protocol_notes,
 NULL::text AS validation_notes, NULL::text AS experiment_phase WHERE false
/*__EXPERIMENT_ROWS_END__*/
)
SELECT em.experiment_id,em.session_id,em.trial_number,em.pair_id,a.name AS application_name,
 em.application_package,em.scenario,em.network_profile,em.device_model,em.app_version,
 em.capture_started_at_utc,em.capture_finished_at_utc,em.test_file_size_bytes,
 nar.capture_duration_ms,nar.observed_primary_payload_span_ms,
 CASE WHEN nc.status='analyzed' AND em.transfer_completed AND em.capture_isolation_confirmed
   AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
   AND nar.comparison_ready AND nar.direction_metadata_available AND nar.truncated_packet_count=0
   AND nar.observed_primary_payload_span_ms>0
   THEN nar.effective_file_throughput_mbps END AS effective_file_throughput_mbps,
 nar.average_network_throughput_mbps,
 CASE WHEN nc.status='analyzed' AND em.transfer_completed AND em.capture_isolation_confirmed
   AND nc.transfer_file_size_bytes=em.test_file_size_bytes
   THEN nar.total_transfer_amplification_ratio END AS total_transfer_amplification_ratio,
 CASE WHEN nar.capture_id IS NULL THEN 'analysis_missing'
      WHEN NOT nar.comparison_ready THEN 'comparison_not_ready'
      WHEN nar.truncated_packet_count>0 THEN 'truncated_packets'
      WHEN NOT nar.direction_metadata_available THEN 'direction_metadata_missing'
      ELSE 'eligible' END AS throughput_eligibility
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
ORDER BY em.experiment_id,em.session_id,em.application_package,em.scenario,em.trial_number,em.capture_id;
