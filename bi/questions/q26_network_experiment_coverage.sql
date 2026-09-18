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
), capture_fact AS (
 SELECT em.*, a.name AS application_name, nc.status AS capture_status,
        nc.transfer_file_size_bytes AS captured_transfer_size_bytes,
        nar.capture_id IS NOT NULL AS analysis_available, nar.comparison_ready,
        nar.direction_metadata_available, nar.truncated_packet_count,
        nar.observed_primary_payload_span_ms, nar.effective_file_throughput_mbps,
        nar.total_transfer_amplification_ratio, nar.ip_transport_header_overhead_ratio,
        nar.tcp_present, nar.tcp_recovery_tax
 FROM experiment_manifest em
 LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
), metric_coverage AS (
 SELECT cf.*, metric_name, eligible,
        CASE WHEN eligible THEN NULL
             WHEN capture_status IS NULL THEN 'capture_not_found'
             WHEN capture_status<>'analyzed' THEN 'capture_not_analyzed'
             WHEN NOT transfer_completed THEN 'transfer_incomplete'
             WHEN captured_transfer_size_bytes<>test_file_size_bytes THEN 'transfer_size_mismatch'
             WHEN NOT capture_isolation_confirmed THEN 'capture_isolation_unconfirmed'
             WHEN NOT cache_cleared_or_download_verified THEN 'cache_or_download_verification_missing'
             WHEN NOT analysis_available THEN 'analysis_missing'
             WHEN truncated_packet_count>0 THEN 'truncated_packets'
             WHEN metric_name='effective_file_throughput_mbps' AND NOT direction_metadata_available THEN 'direction_metadata_missing'
             WHEN metric_name='effective_file_throughput_mbps' AND observed_primary_payload_span_ms IS NULL THEN 'primary_payload_span_missing'
             WHEN metric_name='tcp_recovery_tax' AND NOT tcp_present THEN 'tcp_not_present'
             ELSE 'metric_unavailable' END AS exclusion_reason
 FROM capture_fact cf
 CROSS JOIN LATERAL (VALUES
   ('effective_file_throughput_mbps', capture_status='analyzed' AND transfer_completed
      AND captured_transfer_size_bytes=test_file_size_bytes AND capture_isolation_confirmed
      AND cache_cleared_or_download_verified AND analysis_available AND comparison_ready
      AND direction_metadata_available AND truncated_packet_count=0
      AND observed_primary_payload_span_ms>0 AND effective_file_throughput_mbps IS NOT NULL),
   ('total_transfer_amplification_ratio', capture_status='analyzed' AND transfer_completed
      AND captured_transfer_size_bytes=test_file_size_bytes AND capture_isolation_confirmed
      AND cache_cleared_or_download_verified AND analysis_available
      AND total_transfer_amplification_ratio IS NOT NULL),
   ('ip_transport_header_overhead_ratio', analysis_available
      AND ip_transport_header_overhead_ratio IS NOT NULL),
   ('tcp_recovery_tax', analysis_available AND tcp_present AND tcp_recovery_tax IS NOT NULL)
 ) metric(metric_name,eligible)
)
SELECT experiment_id, session_id, application_name, application_package, scenario, network_profile,
       metric_name, count(DISTINCT capture_id)::bigint AS manifest_capture_count,
       count(DISTINCT capture_id) FILTER (WHERE analysis_available)::bigint AS analyzed_capture_count,
       count(DISTINCT capture_id) FILTER (WHERE eligible)::bigint AS eligible_capture_count,
       string_agg(DISTINCT exclusion_reason, ', ' ORDER BY exclusion_reason)
         FILTER (WHERE exclusion_reason IS NOT NULL) AS exclusion_reasons,
       min(capture_started_at_utc) AS first_capture_at_utc,
       max(capture_finished_at_utc) AS last_capture_at_utc
FROM metric_coverage
WHERE true
[[AND application_package={{application}}]]
[[AND scenario={{scenario}}]]
[[AND experiment_id::text={{experiment}}]]
[[AND session_id::text={{session}}]]
[[AND network_profile={{network_profile}}]]
[[AND capture_started_at_utc::date >= {{start_date}}]]
[[AND capture_started_at_utc::date <= {{end_date}}]]
GROUP BY experiment_id,session_id,application_name,application_package,scenario,network_profile,metric_name
ORDER BY experiment_id,session_id,application_package,scenario,network_profile,metric_name;
