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
), eligible AS (
 SELECT em.*,a.name application_name,nc.status,nc.transfer_file_size_bytes captured_file_size,
        nar.network_bytes_total,nar.ip_transport_header_bytes,
        CASE WHEN nc.status='analyzed' AND em.transfer_completed AND em.capture_isolation_confirmed
          AND em.cache_cleared_or_download_verified AND nc.transfer_file_size_bytes=em.test_file_size_bytes
          THEN nar.total_transfer_amplification_ratio END total_transfer_amplification_ratio,
        CASE WHEN nar.network_bytes_total>0 THEN nar.ip_transport_header_overhead_ratio END
          ip_transport_header_overhead_ratio,
        nar.primary_direction_amplification_ratio,nar.reverse_path_cost_ratio
 FROM experiment_manifest em LEFT JOIN public.applications a ON a.package_name=em.application_package
 LEFT JOIN public.network_captures nc ON nc.id=em.capture_id AND nc.application_id=a.id
 LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id
)
SELECT experiment_id,session_id,trial_number,application_name,application_package,scenario,network_profile,
 capture_started_at_utc,network_bytes_total,test_file_size_bytes,
 total_transfer_amplification_ratio,
 ip_transport_header_overhead_ratio,
 primary_direction_amplification_ratio,reverse_path_cost_ratio,
 (total_transfer_amplification_ratio IS NOT NULL) AS amplification_eligible,
 (ip_transport_header_overhead_ratio IS NOT NULL) AS header_overhead_eligible
FROM eligible
WHERE true
[[AND application_package={{application}}]]
[[AND scenario={{scenario}}]]
[[AND experiment_id::text={{experiment}}]]
[[AND session_id::text={{session}}]]
[[AND network_profile={{network_profile}}]]
[[AND capture_started_at_utc::date >= {{start_date}}]]
[[AND capture_started_at_utc::date <= {{end_date}}]]
ORDER BY experiment_id,session_id,application_package,scenario,trial_number;
