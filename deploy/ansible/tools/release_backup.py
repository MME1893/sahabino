#!/usr/bin/env python3
"""Fail-closed, root-only local snapshot of Sahabino's persistent release state.

PostgreSQL is dumped separately using the existing pg_dump/pg_restore-verified
backup executable.  Kafka, SeaweedFS and observability volumes are snapshotted
ONLY while their owning containers are stopped.  Services that were running are
restarted in finally, including on snapshot failure.  NO volumes are deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime

VOLUMES = ("kafka", "seaweedfs", "grafana", "loki", "alloy")
MOUNT_TARGETS = {
    "kafka": "/var/lib/kafka/data",
    "seaweedfs": "/data",
    "grafana": "/var/lib/grafana",
    "loki": "/loki",
    "alloy": "/var/lib/alloy/data",
}
Run = Callable[..., subprocess.CompletedProcess[str]]
MIN_MARGIN = 1024 * 1024 * 1024


def docker(args: list[str], *, runner: Run = subprocess.run) -> str:
    process = runner(["docker", *args], check=False, capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def service_container(project: str, service: str, *, runner: Run = subprocess.run) -> dict | None:
    ids = docker(
        [
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
        raise RuntimeError(f"Ambiguous containers for {service}")
    if not ids:
        return None
    return json.loads(docker(["inspect", "--type", "container", ids[0]], runner=runner))[0]


def volume_path(
    project: str, service: str, container: dict | None, *, runner: Run = subprocess.run
) -> pathlib.Path | None:
    name = f"{project}_{service}_data"
    result = runner(
        ["docker", "volume", "inspect", name], capture_output=True, text=True, check=False
    )
    if result.returncode:
        if container is not None:
            raise RuntimeError(f"{service} container exists but expected {name} volume is absent")
        return None
    record = json.loads(result.stdout)[0]
    if record.get("Driver") != "local" or record.get("Name") != name:
        raise RuntimeError(f"Unsafe/nonlocal volume: {name}")
    mount = pathlib.Path(record["Mountpoint"])
    if not mount.is_absolute() or mount.is_symlink() or not mount.is_dir():
        raise RuntimeError(f"Invalid host mountpoint for {name}")
    if container is not None and not any(
        m.get("Type") == "volume"
        and m.get("Name") == name
        and m.get("Destination") == MOUNT_TARGETS[service]
        for m in container.get("Mounts", [])
    ):
        # Grafana's /var/lib/grafana may hold additional bind mounts, but the volume
        # still needs to appear as the exact expected named mount.
        raise RuntimeError(f"Volume/container mount mismatch: {service}")
    return mount


def directory_bytes(root: pathlib.Path) -> int:
    total = 0
    for parent, _dirs, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = pathlib.Path(parent) / name
            total += p.lstat().st_size
    return total


def safe_secret(path: pathlib.Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise RuntimeError(f"Secret must be regular and owner-only: {path}")
    return True


def fingerprint(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_tar(source: pathlib.Path, destination: pathlib.Path) -> None:
    partial = destination.with_name(destination.name + ".partial")
    try:
        with tarfile.open(partial, "w", dereference=False, format=tarfile.PAX_FORMAT) as tar:
            tar.add(source, arcname="data", recursive=True)
        with tarfile.open(partial, "r") as tar:
            if not tar.getmembers() or tar.getmembers()[0].name != "data":
                raise RuntimeError(f"Invalid snapshot archive: {destination}")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def backup_secrets(
    paths: list[pathlib.Path], password_file: pathlib.Path, destination: pathlib.Path
) -> list[str]:
    if not safe_secret(password_file):
        raise RuntimeError(
            "Ansible Vault password file missing or unsafe; cannot encrypt secrets backup"
        )
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required to encrypt and verify the secrets backup")
    existing = [p for p in paths if safe_secret(p)]
    if not existing:
        return []
    ciphertext = destination.with_name(destination.name + ".partial")
    args = ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000"]
    # Stream directly into OpenSSL. Never write plaintext credentials to a
    # temporary file; a SIGKILL must not strand such a plaintext file on disk.
    encrypt = subprocess.Popen(
        [*args, "-pass", f"file:{password_file}", "-out", str(ciphertext)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert encrypt.stdin is not None
        with tarfile.open(fileobj=encrypt.stdin, mode="w|") as tar:
            for index, p in enumerate(existing):
                tar.add(p, arcname=f"secrets/{index:02d}-{p.name}", recursive=False)
        encrypt.stdin.close()
        error = encrypt.stderr.read() if encrypt.stderr is not None else b""
        if encrypt.wait() != 0:
            raise RuntimeError(f"OpenSSL encryption failed: {error.decode(errors='replace')}")
        decrypt = subprocess.Popen(
            [*args, "-d", "-pass", f"file:{password_file}", "-in", str(ciphertext)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert decrypt.stdout is not None
            with tarfile.open(fileobj=decrypt.stdout, mode="r|") as tar:
                count = sum(1 for _ in tar)
            decrypt.stdout.close()
            error = decrypt.stderr.read() if decrypt.stderr is not None else b""
            if decrypt.wait() != 0 or count != len(existing):
                raise RuntimeError(
                    f"Encrypted secrets verification failed: {error.decode(errors='replace')}"
                )
        finally:
            if decrypt.poll() is None:
                decrypt.kill()
                decrypt.wait()
        os.replace(ciphertext, destination)
        return [str(p) for p in existing]
    finally:
        if encrypt.poll() is None:
            encrypt.kill()
            encrypt.wait()
        ciphertext.unlink(missing_ok=True)


def gather(
    project: str, *, runner: Run = subprocess.run
) -> list[tuple[str, pathlib.Path, dict | None]]:
    found = []
    for service in VOLUMES:
        container = service_container(project, service, runner=runner)
        volume = volume_path(project, service, container, runner=runner)
        if volume is not None:
            found.append((service, volume, container))
    return found


def capacity(
    items: list[tuple[str, pathlib.Path, dict | None]],
    secrets: list[pathlib.Path],
    pg_dump: pathlib.Path,
    destination: pathlib.Path,
    margin: int,
) -> tuple[int, int]:
    if not pg_dump.is_file() or pg_dump.stat().st_size == 0:
        raise RuntimeError("Missing or empty VERIFIED PostgreSQL pre-deploy backup")
    size = sum(directory_bytes(mount) for _, mount, _ in items)
    size += pg_dump.stat().st_size
    size += sum(p.stat().st_size for p in secrets if safe_secret(p))
    # Count two copies of the pg_dump: the original under postgres/ and the
    # bundle copy. Require 25% headroom plus an independent fixed reserve.
    needed = int(size * 1.25) + max(margin, MIN_MARGIN)
    free = shutil.disk_usage(destination).free
    if needed > free:
        raise RuntimeError(
            f"Insufficient local backup disk: need={needed} free={free}; "
            "aborting before service stop"
        )
    return needed, free


def preflight(
    project: str,
    destination: pathlib.Path,
    margin: int,
    app: pathlib.Path | None = None,
    password_file: pathlib.Path | None = None,
    *,
    runner: Run = subprocess.run,
) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Release capacity preflight must run as root")
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError("Root-only release backup directory was not provisioned")
    if password_file is not None and (
        not safe_secret(password_file) or shutil.which("openssl") is None
    ):
        raise RuntimeError("Encrypted secret backup prerequisite unavailable")
    if app is not None:
        for secret in (
            app / ".env",
            app / "deploy/ansible/group_vars/production/vault.yml",
            app.parent / "runtime/seaweedfs/seaweedfs-s3.json",
        ):
            safe_secret(secret)
    items = gather(project, runner=runner)
    estimate = sum(directory_bytes(mount) for _, mount, _ in items)
    # Include the physical PostgreSQL dataset as a conservative allowance for
    # pg_dump; the exact second pg_dump will be measured again at snapshot time.
    pg = runner(
        ["docker", "volume", "inspect", f"{project}_postgres_data"],
        capture_output=True,
        text=True,
        check=False,
    )
    if pg.returncode == 0:
        record = json.loads(pg.stdout)[0]
        if record.get("Driver") != "local" or pathlib.Path(record["Mountpoint"]).is_symlink():
            raise RuntimeError("Unsafe PostgreSQL backup source")
        estimate += directory_bytes(pathlib.Path(record["Mountpoint"]))
    need = int(estimate * 1.5) + max(margin, MIN_MARGIN)
    free = shutil.disk_usage(destination).free
    print(f"LOCAL_BACKUP_PREFLIGHT: need={need} free={free}; no offsite copy", flush=True)
    if need > free:
        raise RuntimeError("Insufficient free space for local backups before downtime")


def create_snapshot(
    project: str,
    backup_root: pathlib.Path,
    app: pathlib.Path,
    pg_dump: pathlib.Path,
    password_file: pathlib.Path,
    margin: int,
    *,
    runner: Run = subprocess.run,
) -> pathlib.Path:
    if os.geteuid() != 0:
        raise RuntimeError("Full persistent-data snapshot must run as root")
    if backup_root.is_symlink():
        raise RuntimeError("Backup root must not be a symlink")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    if app.is_symlink() or not app.is_dir():
        raise RuntimeError("Unsafe application directory")
    secrets = [
        app / ".env",
        app / "deploy/ansible/group_vars/production/vault.yml",
        app.parent / "runtime/seaweedfs/seaweedfs-s3.json",
        password_file,
        pathlib.Path("/etc/sahabino/postgres-backup.env"),
        pathlib.Path("/home/sahabino/.ssh/sahabino_github"),
        pathlib.Path("/home/sahabino/.ssh/config"),
    ]
    # Secret permissions and encryption availability are checked before stop.
    if not safe_secret(password_file) or shutil.which("openssl") is None:
        raise RuntimeError("Vault password / openssl needed for encrypted secret backup")
    items = gather(project, runner=runner)
    needed, free = capacity(items, secrets, pg_dump, backup_root, margin)
    print(f"LOCAL_ONLY_BACKUP_WARNING: no offsite copy; need={needed} free={free}", flush=True)
    bundle = backup_root / (
        "release-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())
    )
    bundle.mkdir(mode=0o700)
    previously_running: list[str] = []
    completed = False
    try:
        # Caller already stopped application writers. Freeze other persistent
        # writers for coherent volume snapshots, restore original run states.
        for _service, _mount, container in items:
            if container and container["State"]["Running"]:
                docker(["stop", "--time", "60", container["Id"]], runner=runner)
                previously_running.append(container["Id"])
        records: dict[str, object] = {
            "project": project,
            "local_only": True,
            "volumes": {},
            "postgres": {},
            "secrets": {},
        }
        postgres_copy = bundle / "postgres.dump"
        shutil.copyfile(pg_dump, postgres_copy)
        records["postgres"] = {"file": postgres_copy.name, "sha256": fingerprint(postgres_copy)}
        for service, mount, _container in items:
            archive = bundle / f"{service}.tar"
            make_tar(mount, archive)
            records["volumes"][service] = {"file": archive.name, "sha256": fingerprint(archive)}
        secret_archive = bundle / "secrets.tar.enc"
        saved_paths = backup_secrets(secrets, password_file, secret_archive)
        records["secrets"] = {
            "file": secret_archive.name if saved_paths else None,
            "sha256": fingerprint(secret_archive) if saved_paths else None,
            "sources": saved_paths,
        }
        manifest = bundle / "manifest.json"
        manifest.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(manifest, 0o600)
        completed = True
    finally:
        failures: list[str] = []
        # Dependency order: broker/storage first, then log sink, then readers.
        rank = {"kafka": 0, "seaweedfs": 1, "loki": 2, "grafana": 3, "alloy": 4}
        service_by_id = {container["Id"]: service for service, _, container in items if container}
        for ident in sorted(previously_running, key=lambda cid: rank.get(service_by_id[cid], 99)):
            try:
                docker(["start", ident], runner=runner)
            except RuntimeError as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError(
                "Failed to restart originally running persistent containers: " + "; ".join(failures)
            )
        if not completed:
            shutil.rmtree(bundle)
    print(f"BACKUP_VERIFIED:{bundle}")
    return bundle


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--backup-root", type=pathlib.Path, required=True)
    parser.add_argument("--app", type=pathlib.Path, required=True)
    parser.add_argument("--pg-dump", type=pathlib.Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--vault-password-file", type=pathlib.Path, required=True)
    parser.add_argument("--margin-bytes", type=int, default=MIN_MARGIN)
    args = parser.parse_args()
    try:
        if args.preflight_only:
            preflight(
                args.project,
                args.backup_root,
                args.margin_bytes,
                args.app,
                args.vault_password_file,
            )
        else:
            if args.pg_dump is None:
                parser.error("--pg-dump is required for a release snapshot")
            create_snapshot(
                args.project,
                args.backup_root,
                args.app,
                args.pg_dump,
                args.vault_password_file,
                args.margin_bytes,
            )
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ValueError,
        tarfile.TarError,
    ) as exc:
        print(f"FAIL_CLOSED_BACKUP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
