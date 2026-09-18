# Controlled Network Benchmark collection guide

This is an operator checklist, not automation. The BI module does not capture traffic, upload PCAPs, start analyzers or alter application services.

## Before collection

1. Create a private `experiments/experiment_manifest.json` from the example and set controlled `allowed_device_models`, `allowed_network_profiles` and each exact two-package `allowed_comparison_cohorts` entry. Keep mode 0600 and never commit it.
2. Define one immutable test file per comparison cohort. Record SHA-256 and exact byte length before trials. The SHA is the transferred test-file identity, not the PCAP hash.
3. Record app version, device/Android version, network type/profile, capture tool/version, cache/download verification procedure, isolation procedure and protocol notes. A label such as “wifi” is insufficient unless the named network profile defines the controlled conditions.
4. Allocate UUIDs for experiment, session and each pair before capture. Both sides of a pair must use the same experiment and session identity and an allowed comparison cohort. `capture_id` must be the UUID returned by Sahabino’s authorized network-capture workflow; never infer it from filename, trial number or timestamp.
5. Use the initial balanced plan: 4 apps × 2 scenarios × 5 trials = 40 captures. Randomize or alternate app order within a session where practical, keep file/device/network conditions fixed, and do not stop at five if a documented rerun is necessary. Reruns receive a new trial number and capture ID with an explanation.

## PCAPdroid and transfer evidence

Record PCAPdroid (or other tool) name/version, capture start and finish in UTC, device model, Android version, network profile, package under test and whether capture isolation was confirmed. Confirm the entire requested upload/download completed and that cache was cleared or the download was independently verified. Do not put original filenames, object storage keys, credentials, packet payloads or unrelated application traffic in the BI manifest.

After the source workflow returns `capture_id`, record it exactly. The DB-aware validator checks that the source capture exists and matches application package, scenario, transfer-file size and analyzed status. It deliberately does not treat source PCAP SHA as the test-file SHA because those hash different artifacts.

## Validation and handoff

```bash
python3 scripts/manifest_tool.py \
  --experiment-manifest experiments/experiment_manifest.json \
  --release-manifest experiments/release_manifest.json
# Output must say DB_CHECK_STATUS=NOT_RUN in offline mode.

python3 scripts/manifest_tool.py --db-container "$PG_CONTAINER_ID" \
  --reader-password-file secrets/bi_reader_password \
  --experiment-manifest experiments/experiment_manifest.json \
  --release-manifest experiments/release_manifest.json
```

Stop on duplicate capture/trial, unsupported scenario, timestamp reversal, unapproved device/profile, incomplete transfer, size mismatch, source attribution mismatch or invalid pair. Do not “fix” a mismatch by editing source tables or broadening grants.

Q31 never combines different experiment/session/scenario/file/size/app-version/device/Android/network/tool/phase conditions. At least three independently eligible capture IDs are required separately for each metric. The validator uses the raw test-file hash to derive an opaque file-cohort ID, but raw hashes and free-form notes are not persisted in Saved Question SQL.

Run sync `plan` with the same two manifest paths, inspect capability output and query updates, then use explicit `apply` only after authorization. Content sync is distinct from dashboard data refresh: Metabase subsequently executes saved questions against the reader connection. Missing manifest entries do not delete previously managed cards; if capability is removed, restrict/hide stale dashboards and investigate.

## Release events

The private release manifest preserves evidence source, reference, precision and verification for validation, while source/reference/notes are not compiled into Metabase. A verified day-precision date with blank windows receives the documented default: 14 days before, release day as transition, 14 days after. Otherwise no window is invented. Explicit windows override the default. Network before/after additionally requires expected previous/new app versions and matching scenario, opaque file cohort/size, device/Android, network type/profile and capture tool/version, with at least three eligible captures in each period for each displayed metric.

Backup the Metabase metadata DB, sync state and private manifest/evidence together under separate encrypted retention from Sahabino application backups. Rollback restores a matching metadata/state/key set; it never changes source data or application migrations.
