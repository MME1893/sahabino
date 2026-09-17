# Sahabino deployment: end-to-end audit and regression handoff

**Audited source:** `fix/production-deployment`, GitHub commit `dfd2c5fa4b4d6dc9ce428ff1db967256337b04ef`, plus changes in this deliverable. This is a source review and local regression suite, **not** an execution against the production VPS. The VPS state is not changed by this deliverable.

## Current incident / do not confuse a failed verify with a failed migration

The operator's latest log shows the complete local snapshot, PostgreSQL/Alembic migration, Kafka topics, application and observability container recreation and initial API check all succeeded. The old Ansible runtime verifier failed on `sh: line 2: 2: parameter not set`, caused by escaping `awk ... $2` through a YAML/Jinja/shell string. The rescue handler subsequently **stopped application writers**. No automatic database restore or image rollback took place; this was a failed release and the old success marker must not be overwritten. The running state must be validated in the next deployment; do not manually delete volumes, blindly restart writers or run a database restore.

## Root causes and corrections

| Area | Finding | Correction and regression |
| --- | --- | --- |
| Ansible SeaweedFS verify | Escaping shell single quotes in a Jinja expression corrupted `awk`'s `$2`; deterministic verify failure happened *after migration*. | Move exact PID 1, `/data` and staged credential checks into `infrastructure/network/seaweedfs-entrypoint.sh --verify-runtime`; Ansible release **before maintenance**, Ansible verify and Bash final verification use the same script. Jinja-native rendered-argv and shell syntax regression tests. |
| SeaweedFS single-file bind drift | Existing SeaweedFS container retained the previous single-file entrypoint after Git checkout; the previous script forwarded the newly added `--verify-runtime` flag to `weed`, causing `flag provided but not defined`. | Run the exact checked-out verifier through `docker compose exec -T --user 0 seaweedfs sh -s -- --verify-runtime` with its source supplied on stdin for both Ansible audits and launcher verification. No pre-backup recreation, no volume change. Test old mounted script versus fresh stdin CLI. |
| Release maintenance | Snapshot capacity was assessed after expensive images had already built. | Check capacity before pulls/builds, repeat after build, and retain full check before cold snapshot. Never reduce the backup margin or delete historical backups automatically. |
| Persistent container recovery | An existing labelled PostgreSQL/Kafka/SeaweedFS container was accepted and started without checking the actual persisted volume attachment. | Require precisely the expected named, writable volume at its expected data path before starting or trusting it. Missing/changed mount fails closed. Missing containers still use validated orphan-data and cold-snapshot recovery. |
| CLI argument boundaries | Prior `argparse.REMAINDER` swallowed options intended for helper scripts. | All Compose-wrapping helpers split options on explicit `--` and have positional-mode regression coverage; `-h/--help` works independently of separator. |
| Crawler data safety | Missing crawler container used to be interpreted as no active database work. | Query persisted work even if crawler absent; missing table is a first install only for empty public schema. Incomplete populated schema, DB failure and timeouts abort. |
| Kafka verification | Consumer group command could hang across retries. | Bound subprocess execution by 35 seconds and preserve overall health deadline. |
| Startup order | `docker compose up --no-deps` disables dependency ordering; workers could restart alongside an unready API. | Release and explicit restore start API first and verify readiness before ingestion/analyzer and crawler. |
| HTTP proxy | Runtime-inherited proxy can redirect localhost checks. | Proxy-free API container self-health, Ansible API/restore requests, SeaweedFS readiness and launcher-local checks. Docker daemon build proxy configuration is preserved. |
| Success marker | Ansible wrote `DEPLOYED_REVISION` before Bash launcher completed its own verification. | Only wrapper finalizes the exact checked-out SHA, atomically, after BOTH Ansible verification and Bash post-deploy checks. `ansible-playbook deploy.yml` alone does not finalize the marker. |
| CI coverage | Simple unit tests did not parse all Ansible/Jinja source or render the error-prone nested command. | Static YAML/Jinja audit, Compose override/volume checks, native rendered argv, shell syntax, CLI help and boundary, marker and fail-closed tests run in deployment-safety CI. |

## Freeze / safety contract preserved

- The selected **remote** branch resolves once to a full commit SHA. The staged release automation is loaded from that SHA before provisioning/checkout.
- PostgreSQL and Kafka named volumes are never automatically removed or replaced. A recovery does not initialize a new empty volume over an existing installation.
- Cold backups include Kafka, SeaweedFS, required observability data, verified PostgreSQL dump and encrypted secrets. Local storage is **not** protection against destruction of the VPS or its disk.
- Active crawler work is drained or explicitly authorized for interruption, otherwise stop. `fail-safe`: failed migration/verify does not automatically restore the DB; writers are stopped after maintenance begins.
- No `docker system prune`, `docker volume prune`, `down -v`, unrelated project container removal or unconditional data-service recreation.
- `DEPLOYED_REVISION` means the **last fully verified release**, not a guarantee that every subsequent incident left runtime unaffected.

## Validation performed on generated source

- `pytest -q tests/deployment`: **121 passed** (includes production incident reproduction guards and cross-file tests).
- `python -m compileall -q deploy/ansible/tools tests/deployment`, `bash -n` on deployment Shell scripts and `sh -n` on SeaweedFS POSIX entrypoint: passed.
- PyYAML parse for all Ansible YAML files and production Compose YAML with `!override`: passed; compile all 284+ Ansible Jinja-containing values: passed.
- **Not executed here:** live `docker compose config` against actual `.env`, Docker/BuildKit, Ansible `--syntax-check` (Ansible unavailable in local environment), external database/Kafka/SeaweedFS integration, and actual VPS deployment. These remain release gates. The workflow `.github/workflows/deployment-safety.yml` runs Ansible syntax checks on push/PR where dependencies are installed.
- Full application unit-test collection was attempted, but unavailable local application dependencies (`confluent_kafka`) prevented collection. Deployment regression tests are isolated and passed; do not misreport the application test suite as green.

## Controlled operational handoff

1. Review the audit and apply ALL changed relative paths together to `fix/production-deployment`; commit/push once. Ensure the GitHub *deployment-safety* workflow is green. The local `~/project_s/sahabino-deploy.sh` launcher may remain at its existing version: it fetches and re-executes the selected branch's staged script; this does not require manually manipulating Docker.
2. After CI succeeds, run a **single** controlled deployment with the operator's normal command from `~/project_s`:

   ```bash
   sudo ./sahabino-deploy.sh --deploy --revision fix/production-deployment --socks-proxy 127.0.0.1:8080
   ```

   The script checks disk/backup capacity and fails before maintenance if inadequate; do not bypass the margin or prune volumes.
3. Require the final success message, all services running and healthy, manifest validation, Alembic head equality, running image IDs matching the target release SHA and the final `DEPLOYED_REVISION` marker. If any gate fails, stop and inspect the error instead of calling this release successful.

**Limits:** Code review, simulated Docker regressions, and syntax checks reduce but cannot eliminate production-only issues. A production-like integration environment and a periodic documented restore drill are needed for stronger assurance, especially for single-broker Kafka and local-only backups.
