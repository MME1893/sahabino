# BI source, grain and KPI contract

This contract is derived from the checked-in ORM, migration `20260912_0004_network_capture_analysis.py`, and analyzer implementation. It is not a claim about production availability. `preflight.py` and sync `plan` must verify schema and column grants using `sahabino_bi_reader`.

## Source grains and time

| Source | Grain | Business time used by BI | Important qualification |
| --- | --- | --- | --- |
| Store snapshot | one `crawl_task_id` | `playstore_app_snapshots.collected_at` | Locale comes from that task, never today’s application registry locale. |
| Review | one stable `reviews.id`; observations are versions | `first_observed_at` for cohorts, `observed_at` for versions | Sampled crawler reviews are not total Google Play reviews. |
| Network capture | one `network_captures.id` | private manifest capture start/finish, checked against source capture | At most one `network_analysis_results` row per capture. |
| Release event | one private `release_event_id` | verified date plus explicit baseline/follow-up/transition windows | Store `store_updated_on` is not accepted as an exact release date. |

Every cross-domain question aggregates each fact independently before joining on application package. Store locale remains in the result. Source timestamps, windows and missingness remain visible; no row implies correlation or causality.

## Network KPI definitions

- Primary transfer speed is analyzer-provided `effective_file_throughput_mbps`: test-file bytes × 8 divided by the observed primary-direction payload span. It requires completed transfer, matching file size, confirmed isolation/cache handling, analyzed capture, no truncation, direction metadata, a positive primary span and `comparison_ready`. `average_network_throughput_mbps` is capture-wide IP throughput and is shown separately.
- `total_transfer_amplification_ratio` is captured network-layer IP bytes divided by transfer-file bytes. It is not PCAP file size. It requires a complete isolated transfer with matching transfer size.
- `ip_transport_header_overhead_ratio` is `(network IP bytes - L4 payload bytes) / network IP bytes`. SQL retains a 0–1 ratio; Metabase may format it as a percentage exactly once.
- TCP initial RTT average/p50 and ACK RTT p95 are different within-capture statistics. Group summaries use `percentile_cont(0.5)` over those per-capture values and name the result “median of per-capture …”; they do not relabel it as a pooled packet percentile.
- TCP retransmission rate is analyzer-indicated retransmitted segments divided by TCP data segments. TCP recovery tax is analyzer-indicated retransmitted TCP payload bytes divided by all TCP payload bytes. These are distinct diagnostics, not packet-loss probability, RFC 6349 efficiency or proof of radio/network loss.
- TCP values are `NULL`/N/A when TCP is absent. QUIC capability fields stay separate; q29 never fabricates a QUIC loss value from TCP dissector indicators.

Eligibility is metric-specific. `comparison_ready=false` excludes effective-throughput comparison but does not erase amplification, overhead, RTT, recovery-tax or protocol/capability evidence when that metric's own prerequisites hold. Q31 reports a separate eligible count and readiness state for throughput, amplification, header overhead, initial RTT, ACK RTT p95 and TCP recovery tax. Every displayed group aggregate stays `NULL` until that metric has at least three valid independent captures.

## Comparison rules

The initial target is 40 controlled captures: Baham, Pinno, Telegram and WhatsApp, each with five uploads and five downloads. Five is a collection target, not a SQL maximum. Q31's exact comparison grain is **application × experiment × session × scenario × opaque file cohort × file size × application version × device model × Android version × network type × network profile × capture tool/version × experiment phase**. Upload and Download can therefore never be pooled, and changing any listed condition starts a separate group. `n_valid` is retained as the throughput-eligible compatibility alias; every metric also has its own eligible count. Fewer than 3 metric-eligible independent capture IDs is insufficient; 3–5 is descriptive only; larger samples remain descriptive unless a separately reviewed statistical protocol is introduced. There are no confidence intervals, superiority labels or composite 0–100 scores.

Paired comparisons use only manifest `pair_id`. The validator requires exactly two independently valid captures, different applications in one explicitly allowed comparison cohort, and equal experiment identity, session, scenario, test-file identity/size, device, Android version, network type/profile, tool/version and experiment phase. Q32 repeats those identity and condition checks in SQL and evaluates throughput and amplification independently for both sides. Trial number alone never creates a pair.

Release comparisons deliberately match **scenario × opaque file cohort/size × device × Android version × network type/profile × capture tool/version** across periods. The before and after experiment IDs, sessions and application versions may differ because they identify the two controlled periods; Q38/Q41 instead enforce the release manifest's exact experiment ID, date window and expected previous/new version within each period. Throughput, amplification and TCP recovery tax each require at least three eligible captures in both periods before their medians or main delta are populated. Observed and excluded counts/reasons remain visible below that threshold.

Historical review state is period-specific. Q35 uses the latest observation strictly before the first-observed cohort day's next UTC midnight. Q40 expands each release into baseline/follow-up periods and uses the latest observation strictly before that period end's next UTC midnight. A later score or sentiment revision cannot rewrite either historical result. When sentiment columns are absent, historical sentiment measures and unavailable counts are `NULL`; when the columns exist but no valid `done` label was available by the cutoff, classified count is zero, unavailable count records the review, and the share remains `NULL` on a zero denominator.

## Capability states

- `NETWORK_SCHEMA_MISSING`: required physical tables/columns absent.
- `NETWORK_GRANTS_MISSING`: schema exists but the reader lacks one or more exact columns.
- `NETWORK_SCHEMA_READY`: schema/grants ready before a data count.
- `NETWORK_EMPTY`: readable schema, zero capture rows.
- `NETWORK_DATA_AVAILABLE`: capture data exists but no verified local experiment was supplied.
- `NETWORK_COMPARISON_INSUFFICIENT`: verified captures exist but no app/scenario/experiment group has 3 eligible transfers.
- `NETWORK_COMPARISON_READY`: at least one group meets the descriptive minimum. This does not mean every metric/group is ready.

Release and experiment metadata states are separate. Sentiment is optional and topic annotations are currently `ANNOTATIONS_UNAVAILABLE`. No negative sentiment is treated as a network complaint.

## Reader surface

The grant example gives column SELECT only. Network capture storage identity (`object_key`, original filename), errors, raw warning payloads and hashes are excluded. Raw review text, author and external review identifiers remain excluded. The role has `default_transaction_read_only=on`, statement/lock/idle timeouts, no CREATE/TEMP, no membership, no ownership and no broad table/database SELECT.

## Private metadata persistence and visibility

Manifest compilation occurs inside the synchronizer process, but it is **not memory-only**. On apply, compiled native SQL is stored in each Metabase Saved Question and the normalized question payload (including that SQL) is checkpointed in private `secrets/metabase_sync_state.json`; metadata database and state-file backups therefore contain it. Metabase administrators and question editors with native-query access can inspect the compiled reporting values. Dashboard viewers receive only the report result unless their Metabase permissions also expose question editing or native SQL.

Compiled experiment fields are limited to opaque capture/experiment/session/pair/cohort/file identifiers plus package, scenario, timestamps, file size, application/device/Android/network/tool conditions, provenance booleans and phase. Compiled release fields are limited to opaque event ID, package, before/after versions, verified date/precision, analysis windows and before/after experiment IDs. Raw transferred-file SHA-256 values, validation/protocol/operational notes, release evidence references/sources and release notes are validator-only and never compiled. Plan/apply logs print capability states and logical object keys, not manifest records or values. Protect Saved Question edit permissions, the Metabase metadata DB, its backups and the synchronization state accordingly.
