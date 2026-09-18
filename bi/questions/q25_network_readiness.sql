WITH runtime_capabilities AS (
/*__CAPABILITY_ROWS_START__*/
SELECT 'OFFLINE_NOT_CHECKED'::text AS network_state,
       'MANIFEST_NOT_SUPPLIED'::text AS experiment_state,
       'MANIFEST_NOT_SUPPLIED'::text AS release_state,
       'NOT_CHECKED'::text AS sentiment_state,
       'ANNOTATIONS_UNAVAILABLE'::text AS topic_state
/*__CAPABILITY_ROWS_END__*/
), experiment_manifest AS (
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
), manifest_summary AS (
 SELECT count(*)::bigint AS manifest_capture_count,
        count(DISTINCT experiment_id)::bigint AS experiment_count,
        count(*) FILTER (WHERE transfer_completed AND capture_isolation_confirmed
          AND cache_cleared_or_download_verified)::bigint AS provenance_complete_count,
        count(*) FILTER (WHERE pair_id IS NOT NULL)::bigint AS paired_capture_count
 FROM experiment_manifest
)
SELECT c.network_state, c.experiment_state, c.release_state, c.sentiment_state, c.topic_state,
       m.manifest_capture_count, m.experiment_count, m.provenance_complete_count,
       m.paired_capture_count,
       CASE
         WHEN c.network_state='NETWORK_SCHEMA_MISSING' THEN 'network tables are not deployed'
         WHEN c.network_state='NETWORK_GRANTS_MISSING' THEN 'reader lacks the exact network columns'
         WHEN c.network_state='NETWORK_EMPTY' THEN 'schema is readable but contains no captures'
         WHEN c.experiment_state<>'VERIFIED_EXPERIMENT_AVAILABLE' THEN 'validated experiment metadata is required'
         WHEN c.network_state='NETWORK_COMPARISON_READY' THEN 'descriptive comparison available, not causal'
         ELSE 'capture-level evidence available, comparison thresholds may be insufficient'
       END AS operator_interpretation
FROM runtime_capabilities c CROSS JOIN manifest_summary m;
