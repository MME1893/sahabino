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
SELECT rm.release_event_id,rm.application_package,rm.release_date,rm.release_date_precision,
 rm.release_date_verified,
 c.network_state,c.experiment_state,c.sentiment_state,c.topic_state,
 concat_ws(', ',
  CASE WHEN NOT rm.release_date_verified THEN 'unverified_release_date' END,
  CASE WHEN rm.release_date_precision<>'day' THEN 'release_date_not_exact_day' END,
  CASE WHEN rm.baseline_start IS NULL OR rm.baseline_end IS NULL THEN 'baseline_window_missing' END,
  CASE WHEN rm.followup_start IS NULL OR rm.followup_end IS NULL THEN 'followup_window_missing' END,
  CASE WHEN rm.experiment_id_before IS NULL OR rm.experiment_id_after IS NULL THEN 'network_experiment_ids_missing' END,
  CASE WHEN c.sentiment_state<>'SENTIMENT_DATA_AVAILABLE' THEN 'sentiment_unavailable' END,
  CASE WHEN c.topic_state<>'TOPIC_ANNOTATIONS_AVAILABLE' THEN 'network_complaint_topics_unavailable' END
 ) AS exclusions,
 'store_updated_on is not used as an exact release timestamp, transition dates are excluded, no causality or superiority inference'::text standing_limitations
FROM release_manifest rm CROSS JOIN runtime_capabilities c
WHERE true
[[AND rm.application_package={{application}}]]
[[AND rm.release_event_id::text={{release_event}}]]
[[AND rm.release_date >= {{start_date}}]]
[[AND rm.release_date <= {{end_date}}]]
ORDER BY rm.release_date,rm.application_package;
