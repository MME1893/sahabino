# Sahabino production deployment

For the source-audit findings, regression coverage, production incident state, and release gates, see [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md).

The recommended production entry point is the hardened deployment assistant:

```bash
sudo deploy/ansible/sahabino-deploy.sh
```

It is an operational wrapper around the Ansible playbooks in this directory.
Ansible remains the source of truth for provisioning, backups, configuration
rendering, migrations, Kafka topic provisioning, service startup, and deployment
safety checks. The wrapper exists so an operator does not need to manually
reconstruct host checks, Git/SSH setup, Vault handling, dependency bootstrap,
permission repair, failure diagnosis, and post-deploy verification.

For day-to-day runtime commands after deployment, see
[`docs/operations/README.md`](../../docs/operations/README.md). For a full
read-only-first service/data/dashboard acceptance walkthrough, see
[`docs/operations/PRODUCTION_ACCEPTANCE.md`](../../docs/operations/PRODUCTION_ACCEPTANCE.md).
The latter separately labels tests that contact external services or modify
database rows and requires explicit approval for them.

## Production scope and safety boundaries

The production automation manages Sahabino resources only. It does **not**:

- change firewall, VPN, X-Ray, or unrelated services;
- touch ports 80/443;
- install TLS or a reverse proxy;
- expose Grafana, Loki, or Alloy publicly;
- publish PostgreSQL or Kafka host ports;
- run `git reset --hard` or `git clean` to hide production changes;
- remove Docker volumes as an automatic recovery action.

The API currently binds to port 8000 according to production variables. A public
bind (`0.0.0.0` or `::`) requires explicit operator acknowledgement.

## Host expectations

The assistant is designed for an Ubuntu production VPS. Ubuntu 24.04 is the
explicitly tested baseline. Newer Ubuntu releases produce a warning and continue
with capability-based checks instead of being hard-blocked. The legacy
`--allow-unsupported-os` option remains accepted for compatibility but is no
longer required to bypass an OS-version allowlist.

Docker Engine and Docker Compose are validated before deployment. Compose
v2.24.4 or newer is required by the production override syntax.

The assistant also inspects disk, RAM, swap, pending reboot state, required host
utilities, reserved Sahabino ports, and Docker packaging. A pending reboot is a
warning; the assistant never reboots the host automatically.

## Quick start

From a checkout on the production VPS:

```bash
chmod 755 deploy/ansible/sahabino-deploy.sh
sudo deploy/ansible/sahabino-deploy.sh
```

On a first run, the wrapper can create/validate the deployment account, GitHub
Deploy Key, repository checkout, Ansible controller, runtime inventory, and
production Vault before running provisioning and deployment.

A successful rerun reuses existing validated state. Fix the failed prerequisite
and execute the same command again; there is no separate checkpoint file that
must be manually edited.

## Modes and options

Show built-in help:

```bash
deploy/ansible/sahabino-deploy.sh --help
```

Main modes:

| Mode | Purpose |
| --- | --- |
| `--full` | Bootstrap + provision + deploy + verify. Default. |
| `--check` | Host/repository/Vault preflight only. |
| `--provision` | Bootstrap as needed and run `provision.yml` only. |
| `--deploy` | Bootstrap as needed, deploy a revision, then verify. |
| `--verify` | Verify the currently running deployment; running it as root can also normalize checkout file permissions. It is **not** strictly read-only. |

Useful options:

| Option | Purpose |
| --- | --- |
| `--revision REV` | Branch, tag, or SHA; resolved to a full commit SHA. |
| `--repository-url URL` | Explicit read-only Git URL for standalone first-run bootstrap. |
| `--yes` / `-y` | Accept safe/default choices where possible. |
| `--confirm-public-api` | Explicitly acknowledge a public API bind. |
| `--object-storage-exposure private\|public` | Select SeaweedFS host exposure; defaults to private. |
| `--object-storage-public-endpoint URL` | Supply the exact external HTTP(S) origin used in presigned URLs. |
| `--confirm-public-object-storage` | Acknowledge public object-storage exposure. |
| `--allow-active-crawl-interruption` | After the full drain timeout, explicitly authorize interruption. |
| `--no-reboot` | Acknowledge a pending reboot warning; does not reboot. |
| `--skip-docker-migration` | Refuse automatic Snap-Docker migration. |
| `--socks-proxy HOST:PORT` | Use a SOCKS5 proxy for dependency downloads only. |
| `--no-socks-proxy` | Disable automatic local SOCKS fallback detection. |

Examples:

```bash
# Full interactive production deployment
sudo deploy/ansible/sahabino-deploy.sh

# Deploy a specific revision
sudo deploy/ansible/sahabino-deploy.sh \
  --deploy \
  --revision origin/main

# Verify only
sudo deploy/ansible/sahabino-deploy.sh --verify

# Non-interactive deployment where public API exposure is intentional
sudo deploy/ansible/sahabino-deploy.sh \
  --yes \
  --confirm-public-api \
  --revision <sha>
```

Relevant environment overrides include:

```text
SAHABINO_REPOSITORY_URL
SAHABINO_DEPLOY_REVISION
SAHABINO_VAULT_PASSWORD
SAHABINO_VAULT_PASSWORD_FILE
SAHABINO_VAULT_PASSWORD_STORE
SAHABINO_CONFIRM_PUBLIC_API
SAHABINO_OBJECT_STORAGE_EXPOSURE
SAHABINO_OBJECT_STORAGE_PUBLIC_ENDPOINT_URL
SAHABINO_CONFIRM_PUBLIC_OBJECT_STORAGE
SAHABINO_ALLOW_ACTIVE_CRAWL_INTERRUPTION
SAHABINO_SOCKS_PROXY
SAHABINO_DEFAULT_SOCKS_PROXY
SAHABINO_PYPI_INDEX_URL
SAHABINO_PIP_TIMEOUT
SAHABINO_PIP_RETRIES
SAHABINO_WORKTREE_UMASK
```

## Dependency downloads and optional SOCKS proxy

The Ansible controller is installed outside the checkout at
`/opt/sahabino/ansible-venv`, with collections under
`/opt/sahabino/ansible-collections`.

When dependency definitions change or the controller cache is absent, the
assistant probes the full PyPI `ansible-core` index response. If the direct route
is healthy, it uses the normal route. If the direct route fails or is too slow,
it checks the conventional loopback endpoint `127.0.0.1:8080` unless automatic
fallback has been disabled.

For an explicit SOCKS route, first create a loopback-only SSH dynamic forward in
a separate terminal:

```bash
ssh \
  -D 127.0.0.1:8080 \
  -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  <proxy-user>@<proxy-host>
```

Then run:

```bash
sudo deploy/ansible/sahabino-deploy.sh \
  --socks-proxy 127.0.0.1:8080
```

Proxying is intentionally limited to `pip` and `ansible-galaxy` through
`proxychains4`. It does not change system proxy settings and does not route Git,
Docker daemon traffic, running Sahabino services, or unrelated VPS traffic.

`pip` uses extended timeout/retry settings. A PyPI read timeout or connection
reset is diagnosed separately from the misleading secondary
`No matching distribution found` error that `pip` can emit after a failed index
read.

## GitHub Deploy Key and SSH policy

The default production key is:

```text
~sahabino/.ssh/sahabino_github
```

The assistant:

1. creates the deployment user's `.ssh` directory with restrictive permissions;
2. generates a dedicated Ed25519 key if requested;
3. regenerates a missing `.pub` file from the private key;
4. verifies that private/public files are the same key pair;
5. prints the SHA-256 public-key fingerprint;
6. pins GitHub host keys in a dedicated `known_hosts.github` file;
7. manages only its marked `Host github-sahabino` SSH block;
8. validates the resolved SSH configuration with `ssh -G`;
9. verifies repository read access with `git ls-remote`.

The managed SSH policy includes `IdentitiesOnly yes`, `BatchMode yes`, a bounded
connect timeout, a dedicated known-hosts file, and `StrictHostKeyChecking yes`.
The assistant never uses `accept-new`.

If the key is new, add only the **public** key in GitHub:

```text
Repository -> Settings -> Deploy keys -> Add deploy key
Allow write access: OFF
```

The private key stays only on the production VPS.

## Repository discovery and checkout safety

The production checkout defaults to:

```text
/opt/sahabino/app
```

Repository discovery checks the invoking checkout, the production checkout, and
project production variables before prompting. GitHub HTTPS/SSH forms are
normalized to the production read-only alias policy where appropriate.

An existing checkout must be readable/writable by the deployment user and must
be clean. The assistant refuses to overwrite local server-side changes. It does
not recursively `chown` arbitrary untracked production content and does not use
destructive Git cleanup commands to make a deployment pass.

The requested revision is fetched and resolved to a full commit SHA before
Ansible receives it. If no revision is supplied, the assistant discovers the
remote default branch from `origin/HEAD`/remote HEAD instead of blindly assuming
`main`.

## Production permission model

The wrapper itself starts with a restrictive `umask 077` for secrets. Git
worktree operations and Ansible execution use a separate worktree umask
(default `022`) so tracked configuration mounted into non-root containers does
not become unreadable.

After checkout, the assistant reconstructs tracked modes from the Git index:

| Path/type | Production mode/policy |
| --- | --- |
| `/opt/sahabino` | `0750` |
| `/opt/sahabino/app` | `0750` |
| tracked directories | `0755` |
| Git mode `100644` files | `0644` |
| Git mode `100755` files | `0755` |
| tracked symlinks | never followed/chmodded |
| `.env` | `0600` |
| encrypted `vault.yml` | `0600` |
| runtime Ansible inventory | `0600` |
| GitHub Deploy Key private key | `0600` |
| deploy-user SSH config | `0600` |
| persisted Vault password | `0600`, root-owned |

Setuid/setgid bits are removed from paths the wrapper normalizes.

The assistant also discovers repository-relative Compose bind sources from the
base and production Compose files and verifies that:

- the source exists;
- it does not escape the checkout through `..` or a symlink;
- every required parent directory is readable/traversable for arbitrary
  non-root container UIDs;
- mounted files are readable;
- directory trees mounted into containers are readable/traversable.

This protects Loki, Alloy, Grafana provisioning/dashboard files, SeaweedFS
configuration, and future repository-relative bind mounts from the same class of
host-permission failure.

After normalization, `git status --porcelain` must still be clean. If changing
modes according to Git metadata dirties the worktree, deployment stops.

## Local Ansible controller layout

Generated controller/runtime state is kept outside the checkout:

```text
/opt/sahabino/app                          # Git checkout
/opt/sahabino/ansible-venv                # Python/Ansible venv
/opt/sahabino/ansible-collections         # installed collections
/opt/sahabino/runtime/production.inventory.yml
/etc/sahabino/ansible-vault-password      # default persisted Vault password
/var/log/sahabino-deploy-assistant/       # protected run/deploy logs
```

The generated local inventory uses `ansible_connection: local` because the
wrapper runs on the production VPS itself.

## Ansible Vault and secrets

The encrypted production Vault is:

```text
deploy/ansible/group_vars/production/vault.yml
```

It must be Git-ignored and untracked. Existing files must start with the
Ansible-Vault header.

For first-run interactive setup, the assistant can generate a Vault password or
accept one supplied by the operator. By default it persists the password at:

```text
/etc/sahabino/ansible-vault-password
```

The store is root-owned, `0600`, outside the Git checkout, and reused on future
runs. A generated password remains hidden by default; the operator may choose a
one-time terminal reveal for an external backup.

If `SAHABINO_VAULT_PASSWORD_FILE` is supplied explicitly, that file wins and is
not implicitly copied. It must be a non-symlink regular file, non-empty,
outside the checkout, owner-only, and owned by an expected user. An explicit
`SAHABINO_VAULT_PASSWORD` is treated as ephemeral for that invocation.

Generated service secrets are never printed. Plaintext Vault content exists only
in a short-lived mode-`0600` temporary file, is serialized with PyYAML
`safe_dump`, encrypted, and then removed.

The Vault currently contains production values for PostgreSQL, Grafana,
SeaweedFS-compatible object storage, and optional Play Store proxy URLs.
SeaweedFS credentials are atomically rendered outside the checkout at
`/opt/sahabino/runtime/seaweedfs/seaweedfs-s3.json`, owned `root:root` with mode
`0600`; its parent secret directory is `root:root` mode `0700`.

## Provisioning and deployment sequence

A full run performs these major stages:

1. Host preflight: Ubuntu capability check, disk/RAM/swap/reboot state.
2. Base host dependency validation/install.
3. Deployment account validation/creation.
4. Docker Engine/Compose validation and Snap-Docker safety handling.
5. GitHub Deploy Key and pinned-host-key SSH policy.
6. Production repository discovery/clone/fetch and clean-worktree checks.
7. Git-index-based checkout permission normalization and Compose bind validation.
8. Reserved Sahabino port conflict checks.
9. Local Ansible venv/collection synchronization, optionally through SOCKS.
10. Runtime local inventory generation outside the repository.
11. Production Vault/password validation or creation.
12. Ansible syntax checks.
13. Idempotent `provision.yml`.
14. Revision resolution to a full Git commit SHA.
15. Explicit public-API and object-storage exposure acknowledgements when applicable.
16. `deploy.yml`.
17. Post-deploy permission, service, readiness, data, lag, and resource checks.

Inside `deploy.yml`, Ansible enforces:

1. parameter/secret/Docker/Compose/Git preflight;
2. a verified PostgreSQL custom-format backup before revision/migration changes;
3. exact revision checkout;
4. atomic mode-`0600` production `.env` and root-protected SeaweedFS credential rendering;
5. merged Compose validation with both `observability` and `network` profiles;
6. application image builds, including the separate network-analyzer image;
7. SeaweedFS startup/readiness and idempotent bucket initialization while the current crawler remains running;
8. a bounded database-backed crawler drain before any writer interruption;
9. PostgreSQL/Kafka startup/readiness;
10. explicit Alembic migration and exact code-head/database-head equality;
11. Kafka topic topology provisioning/verification;
12. application, network-analysis, and observability startup/update;
13. service/readiness verification and deployed-SHA recording.

A failed pre-deploy backup stops the deployment before migrations.

## Post-deploy verification

The wrapper's `--verify` path is also executed after a successful deployment.
It checks:

- production checkout/bind-mount permission policy;
- sensitive file modes;
- all services expected by the merged Compose model are running;
- Kafka container health;
- API `/docs` readiness;
- Loki `/ready` readiness with retries for its normal temporary 503 warm-up;
- Grafana `/api/health`;
- SeaweedFS readiness, non-root PID 1, protected staged credentials, and a read-only bucket HEAD;
- TShark availability in the analyzer image;
- a Kafka 4.3 consumer-group state check requiring `Stable` and at least one live analyzer member;
- PostgreSQL pipeline counts;
- non-succeeded crawler tasks;
- ingestion consumer-group lag;
- recent crawler scheduler markers;
- recent ingestion processing markers;
- resource usage for Sahabino project containers only;
- `/opt/sahabino/DEPLOYED_REVISION` when present.

If a service is missing/non-running, the wrapper prints the service name,
`docker compose ps -a` output for that service, and its last 120 log lines.

Run verification at any time with:

```bash
sudo deploy/ansible/sahabino-deploy.sh --verify
```

## Production network and object storage

The normal production model enables both `observability` and `network`. Network
remains a separate Compose profile, and `network-analyzer` remains a separate
application image; an operator does not run a second deployment to activate it.

Private object storage is the default, including for `--yes`. SeaweedFS port
8333 binds to `127.0.0.1`, while containers always use the internal
`http://seaweedfs:8333` endpoint. Public exposure requires all three explicit
inputs: `--object-storage-exposure public`, a supplied
`--object-storage-public-endpoint`, and
`--confirm-public-object-storage`. URL parsing permits only an HTTP(S) origin
with a valid non-local host and optional valid port; credentials, query strings,
fragments, non-root paths, reserved names, and non-global IP addresses are
rejected. No public URL is guessed. TLS, DNS, reverse proxy, and firewall policy
are not managed here.

The root-readable host secret is never relaxed for the container. The production
override starts as root through
`infrastructure/network/seaweedfs-entrypoint.sh`; the wrapper creates an
ephemeral tmpfs-backed traversable directory, atomically stages a
`root:seaweed` mode-`0640`
copy, proves the `seaweed` process can read but not modify it, and invokes the
pinned image's original `/entrypoint.sh`. The upstream entrypoint retains its
normal `/data` ownership repair and drops to the image's `seaweed` UID/GID 1000.
Compose does not force a `user:` override or fixed host GID.

The entrypoint is a **single-file bind mount**, so an existing container may
retain an earlier file inode after Git checks out a new release. All runtime
security audits therefore stream the exact checked-out entrypoint script over
stdin into `sh -s -- --verify-runtime`; they do not execute the potentially
stale mounted file or recreate the data-bearing container before backups.
Startup continues to use the Compose bind mount as configured.

After SeaweedFS is ready, deployment automatically runs the idempotent
`python -m sahabino.network storage-init` command. Only then does it inspect
`crawl_runs` for `pending` or `running` work. Active work is polled every 10
seconds for up to 600 seconds. A SQL/Compose failure aborts immediately. On an
interactive timeout, abort is the default and interruption requires an explicit
choice; non-interactive deployment aborts unless
`--allow-active-crawl-interruption` was supplied. That override never skips the
normal wait, and a final database check closes the stop race window.

Analyzer health is based on the Kafka 4.3 consumer-group state command executed
inside the Kafka container. Startup/rebalance states and command failures are
retried; only `Stable` with at least one member succeeds. Missing committed
offsets do not by themselves fail a live consumer. Verification creates no
synthetic captures and does not reset offsets or alter topics.

## Public API acknowledgement

When the production API binds to `0.0.0.0` or `::`, interactive deployments
require confirmation. In non-interactive mode use:

```bash
sudo deploy/ansible/sahabino-deploy.sh \
  --yes \
  --confirm-public-api \
  --revision <sha>
```

This acknowledgement does not mean TLS/authentication/reverse-proxy/firewall
protection exists. Those controls are outside this deployment assistant.

## Backups

PostgreSQL custom-format dumps are stored under:

```text
/var/backups/sahabino/postgres
```

Provisioning installs a systemd backup service/timer. Inspect it with:

```bash
sudo systemctl status sahabino-postgres-backup.timer
sudo systemctl list-timers sahabino-postgres-backup.timer
sudo journalctl -u sahabino-postgres-backup.service --since today
```

List archives:

```bash
sudo find /var/backups/sahabino/postgres \
  -maxdepth 1 \
  -type f \
  -name '*.dump' \
  -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' \
  | sort
```

Validate one archive catalog:

```bash
sudo pg_restore --list /var/backups/sahabino/postgres/NAME.dump >/dev/null
```

These PostgreSQL dumps include network metadata and analysis rows, not the raw
PCAP objects stored in the `seaweedfs_data` named volume. The 2026-09-17 local-only release contract below adds consistent stopped-volume
snapshots for pre-migration releases; off-host disaster recovery remains out of scope. Never use Compose `down -v`
as a recovery or restart command; it destroys named-volume data.

Run the Ansible manual-backup playbook using the persisted Vault password:

```bash
cd /opt/sahabino/app/deploy/ansible
sudo env ANSIBLE_COLLECTIONS_PATH=/opt/sahabino/ansible-collections \
  /opt/sahabino/ansible-venv/bin/ansible-playbook \
  -i /opt/sahabino/runtime/production.inventory.yml \
  backup.yml \
  --vault-password-file /etc/sahabino/ansible-vault-password
```

## Guarded restore

Restore is deliberately explicit and should not be used as a routine
troubleshooting shortcut:

```bash
cd /opt/sahabino/app/deploy/ansible
sudo env ANSIBLE_COLLECTIONS_PATH=/opt/sahabino/ansible-collections \
  /opt/sahabino/ansible-venv/bin/ansible-playbook \
  -i /opt/sahabino/runtime/production.inventory.yml \
  restore.yml \
  --vault-password-file /etc/sahabino/ansible-vault-password \
  -e sahabino_restore_dump_path=/var/backups/sahabino/postgres/NAME.dump \
  -e sahabino_restore_confirm=true \
  -e sahabino_restore_confirmation=RESTORE_SAHABINO_PRODUCTION
```

The restore role validates the source archive, stops application writers,
creates a restore-safety backup, restores only the configured Sahabino database,
checks the restored Alembic revision, and then restarts application services.

## Private observability access

Production Grafana, Loki, and Alloy bind to loopback only. From a workstation:

```bash
ssh -N \
  -L 3000:127.0.0.1:3000 \
  -L 3100:127.0.0.1:3100 \
  -L 12345:127.0.0.1:12345 \
  sahabino@SERVER_IP
```

Then use:

```text
Grafana  http://127.0.0.1:3000
Loki     http://127.0.0.1:3100
Alloy    http://127.0.0.1:12345
```

Private production object storage keeps SeaweedFS S3 on loopback port 8333. If
a remote client must follow a presigned URL, add:

```text
-L 8333:127.0.0.1:8333
```

Do not expose these ports publicly just to access the dashboards/storage API.

## Failure logs and reruns

Protected wrapper logs are written under:

```text
/var/log/sahabino-deploy-assistant/
```

The failure classifier provides targeted guidance for common signatures such as:

- dirty Git checkout;
- Docker Snap confinement;
- PyPI read timeout/reset and dependency-install failure;
- Vault/password problems;
- GitHub Deploy Key/SSH failure;
- disk exhaustion;
- OOM conditions;
- pre-deploy backup failure;
- port collisions;
- container health/readiness failure;
- unreadable container-mounted configuration;
- missing/non-running Compose services.

No destructive recovery is applied automatically. Fix the actual prerequisite
and rerun the same wrapper command.

## Manual validation

The wrapper runs Ansible syntax checks itself. From a development checkout, the
merged production Compose model can also be checked without starting services:

```bash
docker compose \
  --project-name sahabino \
  --env-file .env.example \
  --file docker-compose.yml \
  --file compose.prod.yml \
  --profile observability \
  --profile network \
  config --quiet
```

For the complete post-deploy operations/check command set, continue with
[`docs/operations/README.md`](../../docs/operations/README.md).

## Frozen local-only deployment contract (2026-09-17)

The following rules apply to the upgraded deployment assistant in this archive.
**Local backups are not disaster recovery**: loss of this VPS or its disk can
also destroy every backup. Configure off-host replication and perform a restore
drill before relying on it for a production RPO. No automatic database restore
occurs after a failed migration.

### One-time upgrade from the old assistant

An already-installed *old* `sahabino-deploy.sh` cannot execute code that it has
not yet obtained. **Install the new launcher once**, without replacing the
production checkout or touching containers/volumes. For example, transfer the
updated `deploy/ansible/sahabino-deploy.sh` from this package to a temporary
location on the VPS, then install that file as root:

```bash
sudo install -o root -g root -m 0750 /tmp/sahabino-deploy-new.sh /usr/local/sbin/sahabino-deploy
# Example only: select an approved branch/SHA; use --socks-proxy only if required.
sudo /usr/local/sbin/sahabino-deploy --deploy --revision origin/main
```

The temporary source must come from a reviewed and trusted release. Do not
blindly checkout or reset the live repository just to update its launcher.
After the one-time installation, use `/usr/local/sbin/sahabino-deploy` for
subsequent releases. The launcher fetches the remote branch and pins its **full
SHA**, stages that exact revision's deployment code in a root-only temporary
directory, and executes that revision's assistant; it does not reuse a stale
local branch. A deployment lock prevents two release processes from competing.
The new assistant recovers missing PostgreSQL/Kafka containers automatically if
their existing named volumes remain, then makes a verified PostgreSQL dump
*before checkout*. It does not require manual container IDs, `docker compose up`,
or `docker rm`. Production `.env`/Vault files remain intact.
Recovery requires recognizable, compatible persisted state: PostgreSQL
`PG_VERSION`/`global/pg_control` must match the configured image major version;
Kafka `meta.properties` must match the configured cluster and node identities.
For each **orphaned** database volume the assistant first creates a root-only,
SHA-256-verified **cold archive** under
`/var/backups/sahabino/releases/orphan-*/`. It checks local disk capacity with
an additional 1 GiB reserve, and reattaches the same volume without deleting
or overwriting its contents. If a volume is missing while the other database or
SeaweedFS persists, or an existing orphan volume is empty/corrupt/ambiguous,
the assistant fails closed rather than initializing a new empty database.
An unrelated container attached to an orphan volume also blocks recovery.
This recovery also runs when the checked-out SHA already equals the target SHA.
These archives are local-only and are not a substitute for a restore drill.
No explicit revision defaults to the remote's advertised HEAD branch. A SHA
argument deploys that fetched commit rather than resolving a moving branch.

### What happens in each deployment

1. Resolve the remote branch to one immutable SHA; refuse dirty checkouts or
   missing Git/backup prerequisites. Stage the selected automation, recover
   missing database/broker containers from their original volumes (with a cold
   archive first), and create a verified **pre-checkout PostgreSQL dump** when
   the database exists.
2. Checkout the pinned commit, provision the same revision's Ansible/controller,
   render secrets and validate Compose. Build and tag *all* application images
   with the exact full release SHA **before stopping any writers**.
3. Verify local backup capacity ahead of downtime, requiring a minimum 1 GiB
   free-space reserve **in addition to estimated backup space**. Existing
   persistent infrastructure is started (if necessary) but never replaced
   before this backup. The SeaweedFS readiness probe bypasses Docker proxy vars.
4. Drain active crawling according to the existing timeout/explicit-interrupt
   safety gate; stop only application writers by their Compose project/service
   labels. Verify a new `pg_dump`/`pg_restore --list` after writers are stopped.
5. Stop Kafka, SeaweedFS, Grafana, Loki and Alloy **only if previously running**,
   archive their *local-driver named-volume data* while stopped, verify archive
   contents/SHA-256, copy the PostgreSQL dump, and encrypt and verify a local
   Secrets archive with the root-only Vault-password file via OpenSSL PBKDF2.
   The snapshot tool restarts previously running persistent containers in a
   `finally` block, even on archive failure. Disk or snapshot errors stop
   deployment **before database migration**. The writer containers remain
   stopped on failure rather than running incompatible code or schemas.
6. With the successful snapshot manifest in hand, remove only stopped,
   mount-free *application* containers referencing deleted image IDs. A
   separate allowlist permits replacing SeaweedFS/observability containers
   with missing images **only after checking their volume mounts and archive
   checksums**. PostgreSQL and Kafka are never automatically removed.
7. Reconcile SeaweedFS, migrate the DB, verify the exact Alembic head, provision
   Kafka topics, force-recreate only the four application containers, and
   reconcile observability preserving volumes. Check health, Kafka group, bucket,
   and actual running application image IDs against their SHA-pinned image tags.
   `DEPLOYED_REVISION` updates **only after full verification**.

Persistent local artifacts are root-only under:

- PostgreSQL dumps: `/var/backups/sahabino/postgres/`.
- Missing-container cold recovery archives:
  `/var/backups/sahabino/releases/orphan-*/` (one volume archive and checksum
  manifest per recovered PostgreSQL or Kafka container).
- Complete per-release bundles: `/var/backups/sahabino/releases/release-*/`.
  Each bundle contains `postgres.dump`, `kafka.tar`, `seaweedfs.tar`,
  `grafana.tar`, `loki.tar`, `alloy.tar` as applicable, `secrets.tar.enc` and a
  `manifest.json` recording per-file SHA-256 hashes. Missing prior volumes are
  omitted on the first deploy. Some services' volume sizes and snapshot time
  can make the maintenance window significantly longer than a normal restart.

**Important limits:** The SHA-256/`tarfile` and `pg_restore --list` checks verify
archive integrity and the PostgreSQL archive catalog, **not** a proven full
restore or application-level Kafka/SeaweedFS recovery. The local snapshots may
fail when the volume driver is not `local`, paths/mounts differ from the known
production layout, there is insufficient disk, or a required secret is unsafe.
Those failures are deliberate: no best-effort partial release is performed.
The secrets archive requires the separately safeguarded Vault password for
decryption and contains sensitive credentials; never share it or include it in
Git or a support bundle. The checksum manifest and backups themselves are
local and not an off-host immutable backup.

To inspect a backup without modifying production, as root:

```bash
sudo sha256sum /var/backups/sahabino/releases/RELEASE/kafka.tar
sudo tar -tf /var/backups/sahabino/releases/RELEASE/seaweedfs.tar | head
sudo pg_restore --list /var/backups/sahabino/releases/RELEASE/postgres.dump > /dev/null
```

Compare hashes against the **same bundle's** `manifest.json`. A real recovery
must be planned and performed while all corresponding application writers and
persistent containers are stopped, using a *separate* recovery runbook and
controlled destination volumes. Do **not** unpack archives into running Docker
volumes, blindly overwrite existing database files, or assume a code rollback
is safe after an incompatible Alembic migration. Keep the existing explicit
`restore.yml` workflow for PostgreSQL and require a manual, tested recovery plan
for Kafka and SeaweedFS. The script never executes automatic data restore.

Proxy handling: `compose.prod.yml` explicitly supplies empty HTTP(S)/ALL_PROXY
settings for runtime services so Docker CLI build proxies do not leak into
containers. The SeaweedFS healthcheck also strips proxy variables and uses the
loopback master endpoint. Docker daemon/build proxy configuration remains a
host-level prerequisite and is not silently rewritten by the deployment.

Regression coverage: `pytest tests/deployment`, shell `bash -n`, and
`.github/workflows/deployment-safety.yml`. Confirm the existing CI workflow
remains present in the actual checkout; deployment-safety is additive rather
than a replacement. Use the separate production acceptance checklist for live
service and business-output evidence, not only deployment verification.
