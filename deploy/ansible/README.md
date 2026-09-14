# Sahabino production deployment

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
[`docs/operations/README.md`](../../docs/operations/README.md).

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
| `--verify` | Verify the currently running production deployment. |

Useful options:

| Option | Purpose |
| --- | --- |
| `--revision REV` | Branch, tag, or SHA; resolved to a full commit SHA. |
| `--repository-url URL` | Explicit read-only Git URL for standalone first-run bootstrap. |
| `--yes` / `-y` | Accept safe/default choices where possible. |
| `--confirm-public-api` | Explicitly acknowledge a public API bind. |
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
15. Explicit public-API acknowledgement when applicable.
16. `deploy.yml`.
17. Post-deploy permission, service, readiness, data, lag, and resource checks.

Inside `deploy.yml`, Ansible enforces:

1. parameter/secret/Docker/Compose/Git preflight;
2. a verified PostgreSQL custom-format backup before revision/migration changes;
3. exact revision checkout;
4. mode-`0600` production `.env` rendering;
5. merged Compose validation and production exposure/logging assertions;
6. application image build;
7. stopping database-writing application services before migration;
8. PostgreSQL/Kafka startup/readiness;
9. explicit Alembic migration and code-head/database-head equality;
10. Kafka topic topology provisioning/verification;
11. application + observability startup/update;
12. service/readiness verification and deployed-SHA recording.

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

The optional `network` profile also keeps SeaweedFS S3 on loopback port 8333.
If that profile is enabled and a remote client must follow a presigned URL, add:

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
  config --quiet
```

For the complete post-deploy operations/check command set, continue with
[`docs/operations/README.md`](../../docs/operations/README.md).
# Production network subsystem

Normal `--deploy` and `--verify` runs activate both the `observability` and
`network` Compose profiles. The resulting production project contains Postgres,
Kafka, SeaweedFS, API, crawler, ingestion, the separately built
`network-analyzer`, Loki, Alloy, and Grafana. The assistant defaults SeaweedFS
to `127.0.0.1:8333`; public exposure requires an explicit endpoint and
acknowledgement because the assistant configures neither TLS, a reverse proxy,
nor the firewall.

Ansible atomically renders Vault-derived S3 credentials to
`/opt/sahabino/runtime/seaweedfs-s3.json` as root-owned mode `0600`. It starts
SeaweedFS and runs the idempotent `python -m sahabino.network storage-init`
before waiting for persisted active crawl runs to drain. The bounded drain
defaults to abort rather than interrupting work; automation must explicitly use
`--allow-active-crawl-interruption` to override that safety decision.

For private access, forward all operator endpoints from a workstation:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 3100:127.0.0.1:3100 \
  -L 12345:127.0.0.1:12345 -L 8333:127.0.0.1:8333 \
  sahabino@SERVER_IP
```
