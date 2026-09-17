# Standalone review sentiment worker

This directory is intentionally independent of the Sahabino application runtime. The worker reads
and updates `review_observations` directly through psycopg. It does not import Sahabino settings,
consume Kafka, call the crawler or API, or take part in the existing deployment system.

For each `(review_id, exact content)` identity, the worker first reuses a completed result. If no
completed result exists, identical pending observations are grouped and analyzed once. Updates
always include `sentiment_status = 'pending'`, so a restart is safe: a crash before an update leaves
the rows pending, and a crash after commit leaves a reusable completed result. A session-level
PostgreSQL advisory lock permits only one worker process at a time. Model inference happens outside
database transactions.

Persian inference uses the CPU-only pinned revision
`345654b9c84217afec8742bbe1c1bf94e5ac2b5b` of
`HooshvareLab/bert-fa-base-uncased-sentiment-deepsentipers-multi`. Furious and angry probabilities
are summed as negative, neutral remains neutral, and happy and delighted are summed as positive
before selecting the largest combined probability. English inference uses VADER's standard `0.05`
and `-0.05` compound-score thresholds.

## Language policy

`langdetect==1.0.9` runs with `DetectorFactory.seed = 0`. Content must contain at least three
alphabetic characters, the highest-probability language must be `fa` or `en`, and its probability
must meet `SENTIMENT_MIN_LANGUAGE_CONFIDENCE` (default `0.85`). Empty, whitespace-only, emoji-only,
very short, unsupported, uncertain, or detection-error inputs are marked `skipped` with no label.
Mixed Persian/English text is accepted only when the detector confidently selects a supported
language. This deliberately conservative policy can skip valid short reviews and can still
misclassify code-switching text; country and application locale are never used as substitutes.

## Configuration

Copy `.env.example` to a private file outside Git and replace all placeholders. libpq reads
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`. Alternatively, provide
`SENTIMENT_DATABASE_URL`; never place it on the command line or in logs.

Other variables are:

- `SENTIMENT_BATCH_SIZE` (default `32`): bounded pending-row selection size.
- `SENTIMENT_MODEL_BATCH_SIZE` (default `8`): maximum Persian texts per model call.
- `SENTIMENT_MAX_ATTEMPTS` (default `3`): failures before a row becomes `failed`.
- `SENTIMENT_IDLE_SECONDS` (default `900`): watch-mode sleep interval.
- `SENTIMENT_MIN_LANGUAGE_CONFIDENCE` (default `0.85`).
- `SENTIMENT_MODEL_CACHE` (container default `/models/hf-hub`).

The image sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, and model loading also uses
`local_files_only=True`, `trust_remote_code=False`, and safetensors. Startup fails before processing
if the pinned model revision is absent or invalid. The cache is never copied into the image or Git.

## Preflight and manual migration

Do not run the worker before migration `20260917_0007`. The migration is manual and must not be
applied until a verified PostgreSQL backup exists. Perform these checks from a trusted admin
environment that has this checkout, the normal Sahabino Python dependencies, and authorized
database network access:

```sh
git status --short --branch
uv run alembic current
uv run alembic heads
```

Confirm the current revision is `20260916_0006`, the only head is `20260917_0007`, the target is the
intended database, and the backup can be restored. Then apply only the new revision:

```sh
uv run alembic upgrade 20260917_0007
uv run alembic current
```

This command requires the normal `SAHABINO_DATABASE_URL` configuration used by Alembic. Do not put
credentials in shell history. The migration adds columns and constraints only; it does not add
tables or copy `reviews.content` into historical observations. Existing observations become
`skipped`, then the database default changes to `pending` for future inserts. PostgreSQL can add a
non-volatile constant default without rewriting every row on supported versions; constraint
validation still scans the table. The partial pending index does not index review content.

Downgrading to `20260916_0006` preserves observations and their existing composite key and foreign
keys, but permanently drops observation content and all sentiment state. Take another backup and
explicitly accept that data loss before running:

```sh
uv run alembic downgrade 20260916_0006
```

The migration is compatible with the old ingestion image because every new column has either a
nullable shape or a server default. However, the currently running old image does **not** save
observation content or source timestamps. Observations it inserts after migration will be pending
with `content = NULL` and will be skipped by the worker. Apply the migration first; only a later,
manual activation of the modified ingestion code begins saving content for new observations.

## Independent image build and offline preflight

Build from the repository root. Dependency downloads occur at image-build time; model weights do
not. The versions in `requirements.txt` are pinned and isolated from the main project.

```sh
docker build -f sentiment/Dockerfile -t sahabino-sentiment:local .
```

The production host cache is `/srv/sahabino-models/hf-hub`. Before starting the worker, verify that
it contains a snapshot for the exact revision without modifying the cache:

```sh
test -d /srv/sahabino-models/hf-hub
find /srv/sahabino-models/hf-hub -type d -name \
  345654b9c84217afec8742bbe1c1bf94e5ac2b5b -print
```

An empty result is a blocker: populate the cache through a separate, approved model-management
procedure. The worker will not download missing files.

## Discover the database network

PostgreSQL does not expose a host port in production. Discover the running database container and
its attached networks; do not guess a Compose project or network name:

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Networks}}'
docker inspect <postgres-container> \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}'
```

Choose the existing network on which the PostgreSQL service is reachable and set `PGHOST` in the
private environment file to that network-visible service/container DNS name. A read-only cache
mount and explicit existing network are required for both modes.

## Manual execution

`--once` is the default. It processes each pending identity at most once during that invocation,
reports structured counts, and exits. Rows with temporary inference failures stay pending until
the configured attempt limit; run `--once` again to make the next attempt.

```sh
docker run --rm \
  --name sahabino-sentiment-once \
  --network <existing-postgres-network> \
  --env-file /secure/path/sentiment.env \
  --mount type=bind,src=/srv/sahabino-models/hf-hub,dst=/models/hf-hub,readonly \
  sahabino-sentiment:local --once
```

Watch mode processes immediately, drains current work, and sleeps for 900 seconds after a cycle.
Override the interval with `--idle-seconds` or `SENTIMENT_IDLE_SECONDS`:

```sh
docker run --rm \
  --name sahabino-sentiment-watch \
  --network <existing-postgres-network> \
  --env-file /secure/path/sentiment.env \
  --mount type=bind,src=/srv/sahabino-models/hf-hub,dst=/models/hf-hub,readonly \
  sahabino-sentiment:local --watch --idle-seconds 900
```

Stop watch mode gracefully with `docker stop --time 30 sahabino-sentiment-watch`. Closing the
database connection releases the advisory lock. Do not start a second worker to increase
throughput; it will exit because the lock is already held.

## Inspection and controlled retry

Inspect aggregate state without selecting review content:

```sql
SELECT sentiment_status, count(*)
FROM review_observations
GROUP BY sentiment_status
ORDER BY sentiment_status;

SELECT max(sentiment_attempt_count) AS max_attempts,
       count(*) FILTER (WHERE sentiment_status = 'pending') AS pending,
       count(*) FILTER (WHERE sentiment_status = 'failed') AS failed
FROM review_observations;
```

Failed results are never reused. After diagnosing and correcting the cause, retry only an explicitly
reviewed scope in a manual transaction (for example, a reviewed list of `review_id` values):

```sql
BEGIN;
UPDATE review_observations
SET sentiment_status = 'pending',
    sentiment_language = NULL,
    sentiment_label = NULL,
    sentiment_processed_at = NULL,
    sentiment_attempt_count = 0
WHERE sentiment_status = 'failed'
  AND review_id = ANY (ARRAY[/* reviewed IDs */]::bigint[]);
COMMIT;
```

Check the row count before commit. Do not reset all failed rows blindly.

## Tests

Unit tests use fake inference and never load or download ParsBERT:

```sh
python -m pytest sentiment/tests/unit
```

PostgreSQL migration, repository, worker, retry/restart, and advisory-lock coverage uses the existing
integration harness (Docker or `TEST_DATABASE_URL`):

```sh
python -m pytest tests/integration/ingestion
```

The real-model smoke test is separate and opt-in. It still runs offline and requires the mounted
cache:

```sh
RUN_SENTIMENT_MODEL_SMOKE=1 \
SENTIMENT_MODEL_CACHE=/srv/sahabino-models/hf-hub \
python -m pytest sentiment/tests/real/test_parsbert.py
```

Do not count the default skipped real-model test as executed model coverage.
