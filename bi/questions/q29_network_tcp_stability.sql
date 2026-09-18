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
SELECT em.experiment_id,em.session_id,em.trial_number,a.name application_name,
 em.application_package,em.scenario,em.network_profile,em.capture_started_at_utc,
 nar.tcp_present,nar.quic_present,
 CASE WHEN nar.tcp_present THEN nar.tcp_connection_count END AS tcp_connection_count,
 CASE WHEN nar.tcp_present THEN nar.tcp_handshake_success_rate END AS tcp_handshake_success_rate,
 CASE WHEN nar.tcp_present THEN nar.tcp_initial_rtt_avg_ms END AS tcp_initial_rtt_average_ms,
 CASE WHEN nar.tcp_present THEN nar.tcp_initial_rtt_p50_ms END AS tcp_initial_rtt_p50_within_capture_ms,
 CASE WHEN nar.tcp_present THEN nar.tcp_ack_rtt_p95_ms END AS tcp_ack_rtt_p95_within_capture_ms,
 CASE WHEN nar.tcp_present THEN nar.tcp_retransmission_count END AS tcp_retransmitted_segment_indicator_count,
 CASE WHEN nar.tcp_present THEN nar.tcp_retransmission_rate END AS tcp_retransmitted_segment_indicator_ratio,
 CASE WHEN nar.tcp_present THEN nar.tcp_recovery_tax END AS tcp_retransmitted_payload_share_recovery_tax,
 CASE WHEN nar.tcp_present THEN nar.tcp_receiver_stall_ratio END AS tcp_receiver_stall_ratio,
 CASE WHEN nar.tcp_present THEN nar.tcp_reset_rate END AS tcp_reset_flow_ratio,
 CASE WHEN nar.tcp_present THEN 'eligible_tcp_capture'
      WHEN nar.capture_id IS NULL THEN 'analysis_missing'
      WHEN nar.quic_present THEN 'not_applicable_quic_without_tcp'
      ELSE 'tcp_not_observed' END AS tcp_metric_status
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
ORDER BY em.experiment_id,em.session_id,em.application_package,em.scenario,em.trial_number;
