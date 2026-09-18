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
 NULL::text release_date_source,NULL::text release_evidence_reference,NULL::boolean release_date_verified,
 NULL::date baseline_start,NULL::date baseline_end,NULL::date followup_start,NULL::date followup_end,
 NULL::date transition_period_start,NULL::date transition_period_end,NULL::uuid experiment_id_before,
 NULL::uuid experiment_id_after,NULL::text notes WHERE false
/*__RELEASE_ROWS_END__*/
)
SELECT rm.*,c.network_state,c.experiment_state,c.release_state,c.sentiment_state,c.topic_state,
 CASE WHEN rm.release_event_id IS NULL THEN 'release_manifest_missing'
      WHEN NOT rm.release_date_verified THEN 'release_date_unverified'
      WHEN rm.release_date_precision<>'day' THEN 'release_date_not_day_precision'
      WHEN rm.baseline_start IS NULL OR rm.baseline_end IS NULL OR rm.followup_start IS NULL OR rm.followup_end IS NULL
       THEN 'before_after_windows_missing'
      ELSE 'release_metadata_ready_observational_only' END release_readiness
FROM runtime_capabilities c LEFT JOIN release_manifest rm ON true
WHERE true
[[AND rm.application_package={{application}}]]
[[AND rm.release_event_id::text={{release_event}}]]
[[AND rm.release_date >= {{start_date}}]]
[[AND rm.release_date <= {{end_date}}]]
ORDER BY rm.release_date,rm.application_package;
