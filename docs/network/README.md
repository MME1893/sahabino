# Network capture and analysis

Sahabino's network subsystem is an explicit, idempotent pipeline:

```text
metadata API -> presigned S3 PUT -> /complete -> capture-ready Kafka event
             -> TShark analyzer -> analysis-collected Kafka event -> ingestion -> PostgreSQL
```

The API never proxies PCAP bytes. Raw captures live in S3-compatible object
storage; PostgreSQL holds lifecycle state and typed analytical results. The v1
analyzer assumes plaintext application traffic and uses passive observations
only. It does not decrypt TLS or QUIC and does not define a composite quality
score.

## Local quick start

The `network` Compose profile adds SeaweedFS and the separately built analyzer:

```bash
docker compose --profile network build api ingestion network-analyzer
docker compose --profile network up -d postgres kafka seaweedfs
docker compose run --rm api uv run --no-sync alembic upgrade head
docker compose run --rm api uv run --no-sync python -m sahabino.messaging.admin
docker compose --profile network run --rm network-analyzer \
  uv run --no-sync python -m sahabino.network storage-init
docker compose --profile network up -d api ingestion network-analyzer
```

The credentials in `.env.example` and `infrastructure/network/seaweedfs-s3.json`
are development-only. Production should supply unique secrets and private
internal endpoints. `SAHABINO_OBJECT_STORAGE_ENDPOINT_URL` is used by API and
workers; `SAHABINO_OBJECT_STORAGE_PUBLIC_ENDPOINT_URL` is embedded in client
presigned URLs. Both clients use path-style S3 and Signature V4.

Run the bounded end-to-end acceptance workflow with:

```bash
bash scripts/smoke-network-pipeline.sh --build
```

## API workflow

Register or select an application, generate a SHA-256 of a `.pcapng` or `.pcap`,
then create metadata:

```http
POST /network-captures
Idempotency-Key: <UUID>
Content-Type: application/json

{
  "application_id": "<UUID>",
  "scenario": "upload",
  "filename": "sample.pcapng",
  "capture_size_bytes": 12345,
  "transfer_file_size_bytes": 10485760,
  "sha256": "<64 lowercase hex characters>"
}
```

The response contains stable `capture_id`, `analysis_id`, and `ready_event_id`.
When `upload_required` is true, PUT the file directly to `upload_url` with the
capture's declared content type, then call:

```http
POST /network-captures/{capture_id}/complete
```

Completion performs an S3 HEAD and verifies object size before changing state and
publishing. SHA-256 is intentionally verified later from downloaded bytes by the
analyzer. Other endpoints are:

```text
GET  /network-captures?application_id=&scenario=&status=
GET  /network-captures/{capture_id}
POST /network-captures/{capture_id}/download-url
```

Do not log or persist presigned URLs. They contain temporary authorization data.

## Identity and lifecycle

The physical key is content-addressed:

```text
captures/sha256/{first-two-hex}/{sha256}.{pcap|pcapng}
```

One logical capture is unique by `(application_id, scenario, expected_sha256)`.
Every successfully accepted `Idempotency-Key` UUID is stored in a request-key
mapping. Its SHA-256 request fingerprint covers application, scenario, original
filename, capture format, capture size, transfer-file size, and expected hash.
Repeating a key with the same semantic request returns the mapped capture;
changing any fingerprinted field returns HTTP 409. Multiple keys may map to one
capture. A new key for the same application/scenario/hash reuses the logical row
only when capture format, capture size, and transfer-file size match that
canonical capture; a mismatch returns HTTP 409 `capture_metadata_conflict` and
does not bind the new key. Original filenames may differ. A compatible request
durably records the additional key. Another application or scenario may get
another row but shares the verified physical object. An unverified row is never
enough to skip upload. Reuse of verified DB metadata also requires a successful
S3 HEAD whose physical size agrees. Missing objects require a fresh PUT;
inconsistent physical size is a deterministic 409. An expired logical row is
reopened as `pending_upload` with the same capture and event identities, for both
an already-mapped and a newly accepted idempotency key.

Lifecycle states are:

```text
pending_upload -> uploaded -> analyzing -> analyzed
                     |            |
                     +----------> failed -> explicit retry -> uploaded
pending_upload -> expired
```

Ready/analysis event UUIDs and the analysis UUID are allocated with the capture
and never regenerated. Repeated create, complete, reconciliation, or Kafka
redelivery therefore replays a stable identity. An atomic DB update claims an
`uploaded` row. An `analyzing` row can only be reclaimed after
`SAHABINO_NETWORK_STALE_ANALYSIS_SECONDS`, unless a handled transient failure
explicitly returned it to `uploaded`. The attempt count increases and
`analysis_started_at` is refreshed for each successful claim; duplicate
`analyzed` delivery changes neither. Terminal `failed`/`expired` rows are
acknowledged without analysis.

The analyzer writes `analyzing` before download. After successful parsing it
persists the verified hash, publishes the analysis event, marks the capture
`analyzed`, and only then commits the capture-ready Kafka offset. Terminal data
failures (bad magic/format, checksum mismatch, TShark timeout/unparseable data,
or a confirmed missing completed object) are stored as `failed` and
acknowledged. Transient storage, database, Kafka, and unexpected process failures
remain uncommitted for redelivery.

Ingestion claims `analysis_event_id` in `ingested_events`, validates it against
the capture/application/package/scenario/hash, inserts one result, commits the
database transaction, and then commits Kafka. A duplicate event is harmless.

## Kafka contracts

Both topics use `str(application_id)` as the record key and the shared strict
`EventEnvelope` (`extra=forbid`, schema version 1):

```text
network.capture-ready.v1       event_type=network.capture.ready
network.analysis-collected.v1  event_type=network.analysis.collected
```

`NetworkCaptureReadyV1` contains storage and declared capture metadata.
`NetworkAnalysisCollectedV1` keeps nested metric groups on Kafka. Ingestion
flattens those groups into typed `network_analysis_results` columns for SQL and
future dashboards.

## Formats, direction, and comparison readiness

PCAPNG is recommended; classic PCAP is supported. PCAPNG can carry Enhanced
Packet Block direction flags, while classic PCAP commonly cannot. Missing
direction never rejects a capture: TX/RX, primary-direction, remote-IP,
amplification, and effective-file-throughput values become `NULL` and a warning
is recorded.

`comparison_ready` is true only when the capture has packets, positive duration,
no truncated packets, complete direction metadata for IP packets, and a positive
observed primary-payload span. It is a comparability flag, not a quality score.

Zero means an applicable observation was made and its count/value was zero.
`NULL` means the metric was unavailable or inapplicable. In particular, a QUIC
capture has NULL TCP metrics—not zero retransmissions. Generic UDP exposes no
fabricated retransmission or RTT fields.

## Metric definitions

Bytes are IP-layer bytes: IPv4 `ip.len`; IPv6 fixed 40-byte header plus
`ipv6.plen`. Link-layer overhead is excluded. Rates and ratios use raw fractions
(`0..1` where bounded), durations/RTTs use milliseconds, throughput uses decimal
Mbit/s, and MiB uses 1,048,576 bytes. Percentiles use deterministic nearest rank.

Capture/capability metrics include format, sizes, duration, packet/truncation and
warning counts, direction/plaintext/handshake availability, transport presence,
RTT/DNS availability, and `comparison_ready`.

Traffic and protocol metrics include total/directional network and L4 payload
bytes and packets, IP+transport header bytes and overhead share, TCP/identified
QUIC/other-UDP bytes and shares, and IPv4/IPv6 shares. IP signals include
fragment, ECN-CE, and ICMP-error counts/rates.

A flow is a normalized bidirectional `(IP version, endpoint A IP/port, endpoint B
IP/port, transport)` tuple. Flow metrics include flow count, directional remote
IP count, top-flow payload share, the minimum descending flows needed to reach
80% of payload, and connection churn per MiB.

The presentation KPIs use these formulas:

```text
header overhead ratio = (network bytes - L4 payload bytes) / network bytes
average throughput = network bytes * 8 / capture duration seconds / 1,000,000
1-second throughput = bytes in wall-clock second bucket * 8 / 1,000,000
effective file throughput = transfer file bytes * 8 / primary payload span seconds / 1,000,000
primary-direction amplification = primary-direction network bytes / transfer file bytes
total transfer amplification = all network bytes / transfer file bytes
reverse-path cost = reverse-direction network bytes / primary-direction network bytes
top-flow share = largest flow payload / all L4 payload
connection churn per MiB = flow count / (transfer file bytes / 1,048,576)
TCP tail RTT inflation = TCP ACK RTT p95 / TCP ACK RTT p50
TCP retransmission rate = retransmitted data segments / TCP data segments
TCP recovery tax = retransmitted TCP payload bytes / all TCP payload bytes
TCP receiver stall ratio = zero-window duration / summed TCP flow-active duration
```

TCP also records connection/handshake counts, initial RTT average/p50/p95,
TShark ACK RTT min/p50/p95, fast/spurious/total retransmissions, zero-window and
window-full pressure, resets, out-of-order, duplicate ACK, and lost-segment
indicators.

QUIC is identified passively by TShark and records flows, visible versions,
Retry/version-negotiation/0-RTT counts when fields exist, an initial exchange
estimate when both directions and Initial packets are visible, and spin-bit RTT
samples when available. Encrypted passive traffic does not support trustworthy
QUIC loss/retransmission inference, so none is fabricated. Detection may miss
UDP flows and spin-bit use is optional.

Generic UDP records flow/datagram counts, network/payload bytes, payload-size
p50/p95, and bidirectional byte balance. DNS query/response/failure counts and
TShark response-time p50/p95 are best-effort; query names are not extracted or
logged.

## TShark and standalone analysis

The dedicated `Dockerfile.network-analyzer` installs Debian bookworm's TShark
package and persists the exact runtime `tshark_version` with stable
`analyzer_version=1.0.0`. It invokes TShark directly with an argument list,
numeric name resolution, explicit fields, streamed tabular stdout, captured
stderr, no shell, and a bounded kill timer. The worker validates the executable,
version, and critical fields before constructing Kafka clients. Optional fields,
including `tcp.stream`, degrade gracefully; when available, `tcp.stream` is used
for TCP connection identity while generic flow metrics retain the bidirectional
5-tuple definition.

The default capture-size ceiling is 256 MiB. Until aggregation is fully
streaming, parsing also stops deterministically at
`SAHABINO_NETWORK_MAX_PARSED_RECORDS` (default 250,000) with
`capture_resource_limit` instead of retaining an unbounded packet list.

`SAHABINO_NETWORK_ANALYZER_MAX_POLL_INTERVAL_MS` defaults to 900,000 (15
minutes), comfortably above the 120-second default TShark timeout and surrounding
download/publication work. This analyzer-only Kafka lease prevents normal long
analysis from triggering a consumer-group rebalance; ingestion consumer settings
are unchanged.

Analyze a local file without PostgreSQL or Kafka:

```bash
uv run python -m sahabino.network analyze \
  --pcap sample.pcapng --scenario upload --transfer-file-size-bytes 10485760
```

The command emits the same nested metrics model used by the worker. Override the
binary or timeout with `--tshark`, `--timeout-seconds`,
`SAHABINO_NETWORK_TSHARK_PATH`, or
`SAHABINO_NETWORK_TSHARK_TIMEOUT_SECONDS`. The standalone resource ceiling is
also configurable with `--max-parsed-records` or
`SAHABINO_NETWORK_MAX_PARSED_RECORDS`.

## Recovery and cleanup

Commands use stable row/event identities:

```bash
uv run python -m sahabino.network storage-init
uv run python -m sahabino.network reconcile
uv run python -m sahabino.network retry <capture-id>
uv run python -m sahabino.network cleanup <capture-id>
```

Reconciliation checks old pending uploads: an object with matching size becomes
uploaded and is published; a missing/wrong-size object expires. Uploaded rows
and stale analyzing rows are republished. `retry` accepts only `failed` and
returns it to uploaded before republishing. `cleanup` needs PostgreSQL and object
storage, but not Kafka. It is manual, takes the same object-key advisory lock as
capture creation, refuses an object while any logical reference is non-terminal,
deletes once, and marks all terminal references. No automatic retention policy
is implied.

## Tests and troubleshooting

```bash
uv run pytest tests/unit/network
uv run pytest tests/integration/network
uv run pytest tests/system/network -m system
uv run ruff check .
uv run mypy src
docker compose --profile network config
```

The real-TShark system test skips on hosts without `tshark`; run it inside the
analyzer image or use the runtime smoke script. Integration and smoke tests need
a Docker daemon. Common failures:

- 422 on create: invalid suffix, nonpositive size, oversized capture, malformed
  lowercase SHA, or missing UUID header.
- 409: reused idempotency key with a different request, a new key whose format
  or sizes conflict with the canonical logical capture, terminal/invalid state,
  or unsafe cleanup.
- 503: S3 or Kafka unavailable; retry the same create/complete operation.
- Pending forever: verify the public upload endpoint and content-type, then run
  reconciliation after the configured pending threshold.
- TShark failure: inspect `error_code`, analyzer logs, declared format, checksum,
  capture truncation, and TShark field/version compatibility. Logs contain IDs
  and codes, never packet payloads, query names, credentials, or URLs.

Intentional v1 limitations are plaintext-oriented analysis, passive/incomplete
QUIC classification, no QUIC decryption or inferred QUIC loss, best-effort DNS,
direction degradation for classic PCAP, no storage notifications, no live
capture, and no aggregate 0–100 network score.
