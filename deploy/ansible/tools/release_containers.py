#!/usr/bin/env python3
"""Narrow, labelled-container recovery for a Sahabino release.

Never removes infrastructure, volumes, arbitrary project containers or a running
worker.  Call `remove-stale` only *after* the application's writer stop gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime

APPLICATION = ("api", "ingestion", "crawler", "network-analyzer")
DATABASE_VOLUMES = {
    "postgres": ("postgres_data", "/var/lib/postgresql/data"),
    "kafka": ("kafka_data", "/var/lib/kafka/data"),
}
PERSISTENT_MOUNTS = {
    **DATABASE_VOLUMES,
    "seaweedfs": ("seaweedfs_data", "/data"),
}
RECOVERY_RESERVE_BYTES = 1024 * 1024 * 1024
Run = Callable[..., subprocess.CompletedProcess[str]]


def command(argv: list[str], *, runner: Run = subprocess.run) -> str:
    result = runner(argv, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(argv)}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def containers(project: str, service: str, *, runner: Run = subprocess.run) -> list[dict]:
    ids = command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        runner=runner,
    ).splitlines()
    if len(ids) > 1:
        raise RuntimeError(f"Ambiguous {service} containers; no automated deletion")
    if not ids:
        return []
    return json.loads(command(["docker", "inspect", "--type", "container", *ids], runner=runner))


def image_exists(image: str, *, runner: Run = subprocess.run) -> bool:
    result = runner(
        ["docker", "image", "inspect", image],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def stop_application(project: str, *, runner: Run = subprocess.run) -> list[str]:
    stopped: list[str] = []
    for service in APPLICATION:
        for item in containers(project, service, runner=runner):
            if item["State"]["Running"]:
                command(["docker", "stop", "--time", "60", item["Id"]], runner=runner)
                stopped.append(service)
    return stopped


def remove_stale(project: str, *, runner: Run = subprocess.run) -> list[str]:
    removed: list[str] = []
    for service in APPLICATION:
        for item in containers(project, service, runner=runner):
            ident = item["Id"]
            if image_exists(item["Image"], runner=runner):
                continue
            if item["State"]["Running"]:
                raise RuntimeError(f"Refusing to delete RUNNING {service} container {ident}")
            if item.get("Mounts"):
                raise RuntimeError(f"Refusing to delete mounted {service} container {ident}")
            command(["docker", "rm", ident], runner=runner)
            removed.append(service)
    return removed


def remove_stale_persisted(
    project: str, manifest_path: pathlib.Path, *, runner: Run = subprocess.run
) -> list[str]:
    # This path is intentionally disallowed for Postgres and Kafka: no automatic
    # replacement of the database/broker, even if their image IDs are missing.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("project") != project or manifest.get("local_only") is not True:
        raise RuntimeError("Backup manifest/project mismatch")
    removed: list[str] = []
    for service in ("seaweedfs", "loki", "grafana", "alloy"):
        for item in containers(project, service, runner=runner):
            if image_exists(item["Image"], runner=runner):
                continue
            volume_name = f"{project}_{service}_data"
            if not any(
                m.get("Type") == "volume" and m.get("Name") == volume_name
                for m in item.get("Mounts", [])
            ):
                raise RuntimeError(f"Refusing to remove {service} without expected mounted volume")
            record = manifest.get("volumes", {}).get(service)
            if not record:
                raise RuntimeError(f"No verified backup for {service}")
            path = manifest_path.parent / record["file"]
            if not path.is_file():
                raise RuntimeError(f"Missing backup archive for {service}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != record["sha256"]:
                raise RuntimeError(f"Backup checksum mismatch for {service}")
            if item["State"]["Running"]:
                command(["docker", "stop", "--time", "60", item["Id"]], runner=runner)
            command(["docker", "rm", item["Id"]], runner=runner)
            removed.append(service)
    return removed


def orphan_volume(
    project: str, service: str, *, runner: Run = subprocess.run
) -> pathlib.Path | None:
    """Inspect a *known* named volume; never create one as part of inspection."""
    name = f"{project}_{DATABASE_VOLUMES[service][0]}"
    result = runner(
        ["docker", "volume", "inspect", name],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if "No such volume" in result.stderr or "no such volume" in result.stderr.lower():
            return None
        raise RuntimeError(f"Cannot inspect {name}: {result.stderr.strip()}")
    records = json.loads(result.stdout)
    if len(records) != 1:
        raise RuntimeError(f"Ambiguous Docker volume metadata: {name}")
    record = records[0]
    labels = record.get("Labels") or {}
    if (
        record.get("Name") != name
        or record.get("Driver") != "local"
        or labels.get("com.docker.compose.project", project) != project
    ):
        raise RuntimeError(f"Unexpected Docker volume identity/driver: {name}")
    mount = pathlib.Path(record["Mountpoint"])
    if not mount.is_absolute() or mount.is_symlink() or not mount.is_dir():
        raise RuntimeError(f"Unsafe Docker volume mountpoint for {name}")
    return mount


def checked_compose_service(
    project: str, service: str, compose: list[str], *, runner: Run = subprocess.run
) -> dict:
    if compose[:2] != ["docker", "compose"] or "--project-name" not in compose:
        raise RuntimeError("Explicit project-scoped Compose command required for recovery")
    idx = compose.index("--project-name")
    if idx + 1 == len(compose) or compose[idx + 1] != project:
        raise RuntimeError("Compose project does not match the expected persisted volume")
    model = json.loads(command([*compose, "config", "--format", "json"], runner=runner))
    configured = model.get("services", {}).get(service)
    if not isinstance(configured, dict):
        raise RuntimeError(f"Missing Compose service: {service}")
    source, target = DATABASE_VOLUMES[service]
    mounts = configured.get("volumes", [])
    if not any(
        mount.get("type") == "volume"
        and mount.get("source") == source
        and mount.get("target") == target
        and not mount.get("read_only", False)
        for mount in mounts
    ):
        raise RuntimeError(f"Compose {service} does not attach the expected volume at {target}")
    configured_volume = model.get("volumes", {}).get(source, {})
    if configured_volume.get("name", f"{project}_{source}") != f"{project}_{source}":
        raise RuntimeError(f"Compose {service} overrides the persisted volume identity")
    environment = configured.get("environment") or {}
    required = (
        ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
        if service == "postgres"
        else ("CLUSTER_ID", "KAFKA_NODE_ID")
    )
    if any(not str(environment.get(name, "")) for name in required):
        raise RuntimeError(f"Missing production {service} environment; refusing to reattach data")
    return configured


def validate_orphan_data(service: str, mount: pathlib.Path, configured: dict) -> None:
    """Reject empty, incompatible, or ambiguous data before `compose up`."""
    if service == "postgres":
        version = mount / "PG_VERSION"
        pg_control = mount / "global" / "pg_control"
        paths = (mount / "global", mount / "base", mount / "pg_wal")
        if (
            version.is_symlink()
            or not version.is_file()
            or not version.stat().st_size
            or pg_control.is_symlink()
            or not pg_control.is_file()
            or not pg_control.stat().st_size
            or any(path.is_symlink() or not path.is_dir() for path in paths)
        ):
            raise RuntimeError(
                "PostgreSQL orphan volume has no complete existing cluster; refusing initialization"
            )
        image = configured.get("image", "")
        match = re.fullmatch(r"postgres:(\d+)(?:[.-].+)?", image)
        if not match or version.read_text(encoding="ascii").strip() != match.group(1):
            raise RuntimeError("PostgreSQL data major version and configured image do not match")
    elif service == "kafka":
        metadata = mount / "meta.properties"
        if metadata.is_symlink() or not metadata.is_file() or not metadata.stat().st_size:
            raise RuntimeError(
                "Kafka orphan volume lacks KRaft meta.properties; refusing initialization"
            )
        values = dict(
            line.split("=", 1)
            for raw in metadata.read_text(encoding="utf-8").splitlines()
            if (line := raw.strip()) and not line.startswith("#") and "=" in line
        )
        environment = configured.get("environment") or {}
        if (
            not values.get("cluster.id")
            or values.get("cluster.id") != str(environment.get("CLUSTER_ID", ""))
            or values.get("node.id") != str(environment.get("KAFKA_NODE_ID", ""))
        ):
            raise RuntimeError("Kafka cluster/node identity does not match the saved volume")
        if configured.get("image") != "apache/kafka:4.3.1":
            raise RuntimeError("Unexpected Kafka image for persisted KRaft data")


def cold_recovery_snapshot(
    project: str,
    service: str,
    mount: pathlib.Path,
    backup_root: pathlib.Path,
) -> pathlib.Path:
    """A checksum-verified, root-only cold copy before reattaching orphan data."""
    if os.geteuid() != 0:
        raise RuntimeError("Persistent-volume recovery requires root")
    if backup_root.is_symlink():
        raise RuntimeError("Recovery backup root may not be a symlink")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup_root.is_symlink() or backup_root.stat().st_uid != 0:
        raise RuntimeError("Recovery backup root must be root-owned and non-symlink")
    backup_root.chmod(0o700)
    size = sum(
        (pathlib.Path(parent) / name).lstat().st_size
        for parent, _dirs, files in os.walk(mount, followlinks=False)
        for name in files
    )
    available = shutil.disk_usage(backup_root).free
    required = int(size * 1.25) + RECOVERY_RESERVE_BYTES
    if available < required:
        raise RuntimeError(
            f"Insufficient disk for cold {service} recovery snapshot: "
            f"need={required} free={available}"
        )
    bundle = backup_root / (
        f"orphan-{project}-{service}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    )
    bundle.mkdir(mode=0o700)
    archive = bundle / f"{service}.tar"
    partial = bundle / f"{service}.tar.partial"
    try:
        with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT, dereference=False) as tar:
            tar.add(mount, arcname="data", recursive=True)
        partial.chmod(0o600)
        with tarfile.open(partial, "r") as tar:
            names = tar.getnames()
            required_member = "data/PG_VERSION" if service == "postgres" else "data/meta.properties"
            if "data" not in names or required_member not in names:
                raise RuntimeError(f"Incomplete cold recovery archive for {service}")
        os.replace(partial, archive)
        sha = hashlib.sha256()
        with archive.open("rb") as data:
            for chunk in iter(lambda: data.read(1024 * 1024), b""):
                sha.update(chunk)
        manifest = bundle / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "project": project,
                    "service": service,
                    "local_only": True,
                    "archive": archive.name,
                    "sha256": sha.hexdigest(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        return manifest
    except BaseException:
        partial.unlink(missing_ok=True)
        # An incomplete snapshot must never be mistaken for a verified backup.
        shutil.rmtree(bundle)
        raise


def recover_missing_database(
    project: str,
    service: str,
    compose: list[str],
    backup_root: pathlib.Path,
    *,
    runner: Run = subprocess.run,
) -> str:
    """Recover orphan PostgreSQL/Kafka containers without recreating any data."""
    if service not in DATABASE_VOLUMES:
        raise RuntimeError("Orphan recovery is restricted to PostgreSQL and Kafka")
    existing = containers(project, service, runner=runner)
    if existing:
        verify_existing_persistent_mount(project, service, existing[0])
        return f"EXISTING:{service}"
    mount = orphan_volume(project, service, runner=runner)
    if mount is None:
        # A brand-new install is handled by the normal provisioning flow.  If
        # the *other* core database has persisted data, don't silently start a
        # new empty partner and erase the application's history/offset contract.
        partner = "kafka" if service == "postgres" else "postgres"
        if orphan_volume(project, partner, runner=runner) is not None:
            raise RuntimeError(
                f"{service} volume is missing while {partner} volume exists; "
                "refusing new empty state"
            )
        if containers(project, partner, runner=runner):
            raise RuntimeError(
                f"{service} volume is missing while {partner} container exists; "
                "refusing new empty state"
            )
        # An existing capture volume also indicates an existing installation.
        # Do not silently initialize an empty PostgreSQL/Kafka alongside it.
        seaweed = runner(
            ["docker", "volume", "inspect", f"{project}_seaweedfs_data"],
            text=True,
            capture_output=True,
            check=False,
        )
        if seaweed.returncode == 0:
            raise RuntimeError(
                f"{service} volume is missing while SeaweedFS volume exists; "
                "refusing new empty state"
            )
        if "no such volume" not in seaweed.stderr.lower():
            raise RuntimeError(f"Cannot inspect SeaweedFS state: {seaweed.stderr.strip()}")
        return f"NEW_INSTALL:{service}"
    if os.geteuid() != 0:
        raise RuntimeError("Orphan recovery requires root")
    configured = checked_compose_service(project, service, compose, runner=runner)
    validate_orphan_data(service, mount, configured)
    name = f"{project}_{DATABASE_VOLUMES[service][0]}"
    attached = command(
        ["docker", "ps", "-aq", "--filter", f"volume={name}"],
        runner=runner,
    )
    if attached:
        raise RuntimeError(f"Orphan {name} is attached to another container; refusing recovery")
    manifest = cold_recovery_snapshot(project, service, mount, backup_root)
    print(f"COLD_BACKUP_VERIFIED:{manifest}", flush=True)
    image = str(configured.get("image", ""))
    if not image:
        raise RuntimeError(f"Missing pinned {service} image in Compose")
    if not image_exists(image, runner=runner):
        command([*compose, "pull", "--policy", "missing", service], runner=runner)
        if not image_exists(image, runner=runner):
            raise RuntimeError(f"Cannot obtain {service} image after verified cold snapshot")
    command(
        [
            *compose,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--no-recreate",
            service,
        ],
        runner=runner,
    )
    recovered = containers(project, service, runner=runner)
    expected_target = DATABASE_VOLUMES[service][1]
    if len(recovered) != 1 or not any(
        entry.get("Type") == "volume"
        and entry.get("Name") == name
        and entry.get("Destination") == expected_target
        for entry in recovered[0].get("Mounts", [])
    ):
        raise RuntimeError(
            f"Recovered {service} did not attach the original {name}; stop and inspect"
        )
    return f"RECOVERED:{service}:{manifest}"


def verify_existing_persistent_mount(project: str, service: str, item: dict) -> None:
    """Never start an existing infrastructure container bound to unexpected data.

    Container inspection is read-only. A wrong/missing mount is not repaired by
    allowing Compose to initialize a fresh, empty named volume.
    """
    source, target = PERSISTENT_MOUNTS[service]
    expected = f"{project}_{source}"
    mounted = [m for m in item.get("Mounts", []) if m.get("Destination") == target]
    if (
        len(mounted) != 1
        or mounted[0].get("Type") != "volume"
        or mounted[0].get("Name") != expected
        or mounted[0].get("RW") is False
    ):
        raise RuntimeError(
            f"Unsafe {service} data mount; expected writable named volume "
            f"{expected} at {target}; refusing to start or trust this container"
        )


def ensure_infrastructure(
    project: str, service: str, compose: list[str], *, runner: Run = subprocess.run
) -> None:
    if service not in ("postgres", "kafka", "seaweedfs"):
        raise RuntimeError("Only declared persistent infrastructure can be ensured")
    found = containers(project, service, runner=runner)
    if not found:
        if not compose or compose[:2] != ["docker", "compose"]:
            raise RuntimeError("Explicit Compose command required for first creation")
        if service in DATABASE_VOLUMES:
            status = recover_missing_database(
                project,
                service,
                compose,
                pathlib.Path("/var/backups/sahabino/releases"),
                runner=runner,
            )
            if status.startswith("RECOVERED:"):
                print(status)
                return
        command(
            [*compose, "up", "-d", "--no-deps", "--no-build", "--pull", "never", service],
            runner=runner,
        )
    else:
        verify_existing_persistent_mount(project, service, found[0])
        if not found[0]["State"]["Running"]:
            command(["docker", "start", found[0]["Id"]], runner=runner)
    print(f"ENSURED:{service}")


def verify(project: str, revision: str, *, runner: Run = subprocess.run) -> None:
    for service in APPLICATION:
        found = containers(project, service, runner=runner)
        if len(found) != 1 or not found[0]["State"]["Running"]:
            raise RuntimeError(f"Missing or stopped application service: {service}")
        expected = command(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                f"{project}-{service}:{revision}",
            ],
            runner=runner,
        )
        if found[0]["Image"] != expected:
            raise RuntimeError(
                f"Stale image for {service}: running container differs from newly built tag"
            )
        print(f"VERIFIED:{service}:{expected}")


def main() -> int:
    # argparse.REMAINDER is greedy when it follows a positional mode: it also
    # consumes --project/--service placed after the mode.  Keep the Compose
    # invocation behind an explicit `--` delimiter and parse our own flags
    # separately.  All callers (Bash and Ansible) pass mode before their flags.
    argv = sys.argv[1:]
    if "--" in argv:
        boundary = argv.index("--")
        options, compose = argv[:boundary], argv[boundary + 1 :]
    else:
        options, compose = argv, []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "stop-app",
            "remove-stale",
            "remove-stale-persisted",
            "verify",
            "ensure",
            "recover-missing",
        ),
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--backup-manifest", type=pathlib.Path)
    parser.add_argument(
        "--backup-root",
        type=pathlib.Path,
        default=pathlib.Path("/var/backups/sahabino/releases"),
    )
    parser.add_argument("--service", choices=("postgres", "kafka", "seaweedfs"))
    args = parser.parse_args(options)
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not args.project or any(c not in allowed for c in args.project):
        parser.error("Unsafe Compose project name")
    try:
        if args.mode == "stop-app":
            print("STOPPED:" + ",".join(stop_application(args.project)))
        elif args.mode == "remove-stale":
            print("REMOVED:" + ",".join(remove_stale(args.project)))
        elif args.mode == "remove-stale-persisted":
            if not args.backup_manifest or not args.backup_manifest.is_file():
                raise RuntimeError("Verified backup manifest is required")
            removed = remove_stale_persisted(args.project, args.backup_manifest)
            print("REMOVED_PERSISTED:" + ",".join(removed))
        elif args.mode == "ensure":
            ensure_infrastructure(args.project, args.service, compose)
        elif args.mode == "recover-missing":
            if args.service not in DATABASE_VOLUMES:
                raise RuntimeError("Recover-missing requires postgres or kafka")
            print(recover_missing_database(args.project, args.service, compose, args.backup_root))
        else:
            if (
                not args.revision
                or len(args.revision) != 40
                or any(c not in "0123456789abcdefABCDEF" for c in args.revision)
            ):
                raise RuntimeError("Full 40-character release SHA required for verification")
            verify(args.project, args.revision)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
