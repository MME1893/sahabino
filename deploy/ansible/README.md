# Sahabino production deployment

This directory contains the first production deployment path for the single
Sahabino repository. It manages only Sahabino files, containers, backups, and
systemd units. It does not install Docker, alter host networking or firewall
rules, touch ports 80/443, or interact with VPN/X-Ray services.

Production uses `docker-compose.yml` as the base and `compose.prod.yml` as a
small override. PostgreSQL and Kafka have no published host ports. Grafana,
Loki, and Alloy listen only on `127.0.0.1`; the API listens on port 8000 by
default. No reverse proxy is installed.

## 1. Control-machine setup

Run Ansible from Linux, macOS, or WSL with SSH access to the Ubuntu 24.04 VPS.
Install `ansible-core`, install the required collection, and create the local
inventory from the committed example:

```bash
cd deploy/ansible
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
cp inventory/production.example.yml inventory/production.yml
```

Edit `inventory/production.yml` with the VPS address and SSH account. Confirm
the SSH host key before running Ansible. The inventory file is ignored by Git.
The default variables assume the deployment account and group are both named
`sahabino`; change `sahabino_deploy_user` and `sahabino_deploy_group` in
`group_vars/production.yml` if necessary.

The server must already have Docker Engine, Docker Compose v2.24.4 or newer,
and its read-only GitHub Deploy Key configuration. The role verifies Docker but
does not install or reconfigure it. The default SSH remote is
`git@github-sahabino:MME1893/sahabino.git`.

Check connectivity:

```bash
ansible production -m ansible.builtin.ping
```

## 2. GitHub Deploy Key setup on a new server

Create the key as the deployment account that owns `/opt/sahabino/app` (the
default is `sahabino`). Generate a dedicated Ed25519 key and protect the SSH
directory and private key:

```bash
umask 077
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 \
  -f ~/.ssh/sahabino_github \
  -C "sahabino-production-deploy" \
  -N ''
chmod 600 ~/.ssh/sahabino_github
chmod 644 ~/.ssh/sahabino_github.pub
```

In the GitHub repository, open **Settings → Deploy keys**, add the contents of
`~/.ssh/sahabino_github.pub`, and leave **Allow write access** disabled. Only
the public `.pub` file belongs in GitHub.

Create `~/.ssh/config` if needed, apply restrictive permissions, and then merge
the following stanza into it. Do not replace or truncate unrelated SSH host
entries already on the VPS.

```bash
touch ~/.ssh/config
chmod 600 ~/.ssh/config
```

```sshconfig
Host github-sahabino
    HostName github.com
    User git
    IdentityFile ~/.ssh/sahabino_github
    IdentitiesOnly yes
```

Test the alias:

```bash
ssh -T git@github-sahabino
```

GitHub should report successful authentication while noting that shell access
is unavailable. After the repository exists, verify its SSH remote is exactly:

```bash
git -C /opt/sahabino/app remote get-url origin
# expected: git@github-sahabino:MME1893/sahabino.git
```

The private file `~/.ssh/sahabino_github` must remain only on the VPS. Never
commit it, copy it into this repository, embed it in Ansible, or store it in
Ansible Vault.

## 3. Ansible Vault setup

Create the ignored production vault from the example, replace every
`CHANGE_ME` value, and encrypt it. Do not create or commit a vault-password
file unless it is stored outside this repository and protected separately.

```bash
cp group_vars/production/vault.yml.example group_vars/production/vault.yml
ansible-vault encrypt group_vars/production/vault.yml
ansible-vault view group_vars/production/vault.yml
```

Use long URL-safe secret values containing letters, digits, dots, underscores,
or hyphens. This restriction makes both the generated dotenv file and the
PostgreSQL connection URL unambiguous. Proxy URLs are a Vault-protected list and
may be left empty while proxying is disabled.

The operator must provide:

- PostgreSQL database name, user, and a unique password.
- Grafana admin user and a unique password.
- Unique credentials for the VPS-local SeaweedFS S3-compatible endpoint. These
  are not AWS account credentials. The `network` Compose profile remains opt-in
  and is not deployed by the default playbooks.
- Proxy URLs only if Play Store proxying is enabled.
- The correct repository SSH URL, host address, SSH user, and each deployment
  revision.

The rendered `/opt/sahabino/app/.env` is owned by the deployment account with
mode `0600`. It is never committed.

## 4. Local SeaweedFS object storage

Sahabino does not use Amazon S3 or another remote cloud object-storage provider
by default. The existing S3-compatible abstraction talks to a SeaweedFS
container, and capture bytes persist on the same Sahabino VPS:

```text
Sahabino API / network analyzer
        |
        | S3-compatible protocol
        v
SeaweedFS container
        |
        v
seaweedfs_data Docker named volume
        |
        v
local disk on the Sahabino VPS
```

The production object-storage variables mean:

- `sahabino_object_storage_endpoint_url=http://seaweedfs:8333` is the internal
  Docker endpoint used by Sahabino services.
- `sahabino_object_storage_public_endpoint_url=http://127.0.0.1:8333` is the
  endpoint embedded in presigned URLs.
- `sahabino_object_storage_bucket=sahabino-network-captures` is a logical bucket
  hosted by local SeaweedFS.
- `sahabino_object_storage_region=us-east-1` is only an S3 API/AWS Signature V4
  signing-compatibility value. It does not place data in AWS or a US data center.
- The Vault access key and secret authenticate to local SeaweedFS; they are not
  AWS account credentials.
- Actual capture bytes live in the `sahabino_seaweedfs_data` Docker named volume
  on the VPS when the profile is enabled.

No Amazon S3 account or remote object-storage service is required or configured.
The production playbooks intentionally leave the `network` Compose profile
disabled by default. If it is enabled later, keep SeaweedFS loopback-bound and
use an SSH tunnel when a remote client must follow a presigned URL:

```bash
ssh -N \
  -L 8333:127.0.0.1:8333 \
  sahabino@SERVER_IP
```

The local credentials rendered by Ansible must match SeaweedFS's local S3
configuration before enabling that profile. Do not expose port 8333 publicly.

## 5. Provision once

Provisioning installs only `git` and the PostgreSQL client, creates the
Sahabino application/backup/configuration directories, installs the backup
script and systemd units, and enables the timer. It does not install Docker and
does not modify unrelated services.

```bash
ansible-playbook provision.yml --ask-vault-pass --ask-become-pass
```

Omit `--ask-become-pass` when the SSH account has passwordless sudo. Ansible
still loads the encrypted production group variables, so provide the Vault
password during provisioning.

## 6. First deployment and later revisions

Every deployment requires an explicit revision. A full commit SHA is the most
reproducible choice; tags and explicitly named branches are also accepted.

```bash
ansible-playbook deploy.yml \
  --ask-vault-pass \
  -e sahabino_deploy_revision=0123456789abcdef0123456789abcdef01234567
```

Examples for a tag or branch:

```bash
ansible-playbook deploy.yml --ask-vault-pass -e sahabino_deploy_revision=v0.1.0
ansible-playbook deploy.yml --ask-vault-pass -e sahabino_deploy_revision=main
```

Ansible uses the Git module to fetch and check out the supplied revision with
`force: false`; it never performs a blind `git pull` and refuses to overwrite
server-side repository changes. The resolved SHA is written to
`/opt/sahabino/DEPLOYED_REVISION` only after all checks succeed.

The production Compose project is explicitly `sahabino`; Ansible passes that
name to every Compose operation. Containers, the network, and named volumes
therefore consistently use names such as `sahabino-postgres-1`,
`sahabino_default`, `sahabino_postgres_data`, `sahabino_kafka_data`,
`sahabino_grafana_data`, and `sahabino_loki_data`.

The implemented deployment sequence is:

1. Validate inputs, secrets, Docker/Compose versions, Git access, and checkout
   cleanliness.
2. If a Sahabino PostgreSQL container exists, run `pg_dump -Fc`, verify the
   archive with `pg_restore --list`, and abort on any backup failure.
3. Fetch the repository and check out the requested branch, tag, or commit.
4. Render the mode-`0600` production `.env`.
5. Validate the merged Compose model and assert the database, Kafka, logging,
   and loopback-only observability invariants.
6. Build the API, ingestion, and crawler images from the checked-out source.
7. Stop database-writing application containers, then start PostgreSQL and
   Kafka and wait for their health checks.
8. Run `uv run --no-sync alembic upgrade head` explicitly in the API image and
   verify the database revision equals the checked-out code head.
9. Run `uv run --no-sync python -m sahabino.messaging.admin` to create or verify
   the existing project Kafka topic topology.
10. Start/update API, ingestion, crawler, Grafana, Loki, and Alloy; verify API
    health and that every requested container is running.

The pre-deploy backup is completed before Git checkout or migration. A failed
backup stops the play immediately, so migrations cannot run.

## 7. Backups

Backups are PostgreSQL custom-format dumps, not copies of the Docker volume.
They are written outside the repository under
`/var/backups/sahabino/postgres`. Filenames contain a UTC timestamp and a backup
kind; pre-deploy backups also identify the current and requested revisions when
available. The default local retention is 14 days and is configurable with
`sahabino_backup_retention_days`. Retention pruning runs only after a successful
scheduled daily backup. Manual, pre-deploy, and restore-safety backups create
and verify their archives without deleting any existing dump.

If the matching Sahabino PostgreSQL container exists but is stopped, the backup
script starts it long enough to obtain and verify the dump, then returns it to
the stopped state. It never selects a container by a generic name alone.

Create and verify a manual backup:

```bash
ansible-playbook backup.yml --ask-vault-pass
```

Inspect the daily timer and recent service output on the VPS:

```bash
sudo systemctl status sahabino-postgres-backup.timer
sudo systemctl list-timers sahabino-postgres-backup.timer
sudo journalctl -u sahabino-postgres-backup.service --since today
```

List and validate backups:

```bash
sudo find /var/backups/sahabino/postgres -maxdepth 1 -type f -name '*.dump' -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' | sort
sudo pg_restore --list /var/backups/sahabino/postgres/NAME.dump >/dev/null
```

The backup role has an explicit `sahabino_backup_remote_enabled` extension
point, but intentionally rejects `true` because off-site database backups are
not yet implemented.

## 8. Guarded restore

Place the dump under `/var/backups/sahabino/postgres` and validate its path and
archive catalog first. Restore requires both a boolean flag and an exact
confirmation token:

```bash
ansible-playbook restore.yml \
  --ask-vault-pass \
  -e sahabino_restore_dump_path=/var/backups/sahabino/postgres/NAME.dump \
  -e sahabino_restore_confirm=true \
  -e sahabino_restore_confirmation=RESTORE_SAHABINO_PRODUCTION
```

The restore playbook refuses paths outside the managed backup directory. It
validates the dump, stages it safely, stops API/ingestion/crawler, ensures
PostgreSQL is healthy, creates and verifies a `restore-safety` backup, drops and
recreates only the configured Sahabino database, restores with
`pg_restore --exit-on-error`, and checks the restored `alembic_version` against
the checked-out code head. Application services restart only after that check.
If restore or verification fails, the application writers remain stopped; use
the safety backup and inspect the failure before taking further action.
Creating the safety backup never invokes retention pruning, so an older source
dump cannot be removed as a side effect of the restore.

## 9. Operations and SSH forwarding

Use the same base/override pair and project name for manual inspection:

```bash
cd /opt/sahabino/app
sudo docker compose -p sahabino --env-file .env \
  -f docker-compose.yml -f compose.prod.yml ps
sudo docker compose -p sahabino --env-file .env \
  -f docker-compose.yml -f compose.prod.yml logs --tail=200 api ingestion crawler
sudo docker compose -p sahabino --env-file .env \
  -f docker-compose.yml -f compose.prod.yml logs --tail=200 postgres kafka
```

Grafana, Loki, and Alloy are reachable remotely only through SSH forwarding:

```bash
ssh -N \
  -L 3000:127.0.0.1:3000 \
  -L 3100:127.0.0.1:3100 \
  -L 12345:127.0.0.1:12345 \
  sahabino@SERVER_IP
```

Then open Grafana at `http://127.0.0.1:3000`, Loki at
`http://127.0.0.1:3100`, and Alloy at `http://127.0.0.1:12345`. PostgreSQL and
Kafka have no host port mapping in production and should not be forwarded or
exposed publicly.

The API intentionally remains bound to `0.0.0.0:8000` for the current
deployment/demo stage. This automation does not add authentication, TLS,
firewall rules, or a reverse proxy, and it does not use ports 80 or 443.

## 10. Static validation

Run these checks from a checkout before deployment:

```bash
docker compose -p sahabino --env-file .env.example \
  -f docker-compose.yml -f compose.prod.yml \
  --profile observability config --quiet
ansible-playbook -i inventory/production.example.yml --syntax-check provision.yml
ansible-playbook -i inventory/production.example.yml --syntax-check deploy.yml
ansible-playbook -i inventory/production.example.yml --syntax-check backup.yml
ansible-playbook -i inventory/production.example.yml --syntax-check restore.yml
```

`deploy.yml` repeats merged Compose validation on the VPS before building or
migrating and inspects the rendered JSON model for the security invariants.
