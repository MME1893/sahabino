"""Regression coverage for the frozen, local-only, fail-closed release contract."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import tarfile
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = ROOT / "deploy" / "ansible" / "tools"
RELEASE = ROOT / "deploy" / "ansible" / "roles" / "sahabino_deploy" / "tasks" / "release.yml"
ASSISTANT = ROOT / "deploy" / "ansible" / "sahabino-deploy.sh"


def load_tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


containers = load_tool("release_containers")
backup = load_tool("release_backup")


def response(
    argv: list[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeDocker:
    def __init__(self, *, stale: bool = True, running: bool = False, mounts: list | None = None):
        self.calls: list[list[str]] = []
        self.stale = stale
        self.running = running
        self.mounts = [] if mounts is None else mounts

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:3] == ["docker", "ps", "-aq"]:
            service = argv[-1].split("=")[-1]
            return response(argv, stdout="old-id\n" if service == "api" else "")
        if argv[:4] == ["docker", "inspect", "--type", "container"]:
            return response(
                argv,
                stdout=json.dumps(
                    [
                        {
                            "Id": "old-id",
                            "Image": "sha256:gone",
                            "State": {"Running": self.running},
                            "Mounts": self.mounts,
                        }
                    ]
                ),
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            return response(argv, code=1 if self.stale else 0)
        if argv[:2] == ["docker", "rm"]:
            return response(argv, stdout="old-id")
        raise AssertionError(argv)


def test_only_stopped_mount_free_application_container_is_removed() -> None:
    runner = FakeDocker()
    assert containers.remove_stale("sahabino", runner=runner) == ["api"]
    assert ["docker", "rm", "old-id"] in runner.calls


@pytest.mark.parametrize(
    "running,mounts", [(True, []), (False, [{"Type": "volume", "Name": "data"}])]
)
def test_never_delete_running_or_mounted_application_container(running: bool, mounts: list) -> None:
    runner = FakeDocker(running=running, mounts=mounts)
    with pytest.raises(RuntimeError, match="Refusing"):
        containers.remove_stale("sahabino", runner=runner)
    assert not any(call[:2] == ["docker", "rm"] for call in runner.calls)


def test_missing_image_on_other_service_does_not_block_infrastructure_ensure() -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return response(argv, stdout="sea-id\n")
            if argv[:4] == ["docker", "inspect", "--type", "container"]:
                return response(
                    argv,
                    stdout=json.dumps(
                        [
                            {
                                "Id": "sea-id",
                                "State": {"Running": True},
                                "Mounts": [
                                    {
                                        "Type": "volume",
                                        "Name": "sahabino_seaweedfs_data",
                                        "Destination": "/data",
                                        "RW": True,
                                    }
                                ],
                            }
                        ]
                    ),
                )
            raise AssertionError(argv)

    runner = Runner()
    containers.ensure_infrastructure("sahabino", "seaweedfs", ["docker", "compose"], runner=runner)
    assert not any("up" in call or "rm" in call for call in runner.calls)


def test_volume_snapshot_preserves_content_and_checksum(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "volume"
    source.mkdir()
    (source / "capture.pcap").write_bytes(b"existing production content")
    (source / "link").symlink_to("capture.pcap")
    target = tmp_path / "volume.tar"
    backup.make_tar(source, target)
    assert backup.fingerprint(target) == backup.fingerprint(target)
    with tarfile.open(target) as tar:
        assert "data/capture.pcap" in tar.getnames()
        assert tar.getmember("data/link").issym()
    assert (source / "capture.pcap").read_bytes() == b"existing production content"


def test_secret_backup_is_encrypted_and_verified(tmp_path: pathlib.Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL unavailable")
    secret = tmp_path / "production.env"
    secret.write_text("SECRET_VALUE=not-in-plaintext-archive\n")
    secret.chmod(0o600)
    key = tmp_path / "key"
    key.write_text("enough-random-test-password\n")
    key.chmod(0o600)
    result = tmp_path / "secrets.tar.enc"
    assert backup.backup_secrets([secret], key, result) == [str(secret)]
    assert result.is_file()
    assert b"SECRET_VALUE" not in result.read_bytes()
    assert not list(tmp_path.glob("*.partial"))


def test_secrets_backup_rejects_world_readable_files(tmp_path: pathlib.Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("secret")
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="owner-only"):
        backup.safe_secret(secret)


def test_insufficient_space_fails_before_stopping_services(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = tmp_path / "postgres.dump"
    pg.write_bytes(b"pg")
    vol = tmp_path / "data"
    vol.mkdir()
    (vol / "payload").write_bytes(b"important" * 100)
    DiskUsage = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: DiskUsage(1000, 999, 1))
    with pytest.raises(RuntimeError, match="Insufficient"):
        backup.capacity([("kafka", vol, None)], [], pg, tmp_path, backup.MIN_MARGIN)


def test_release_has_snapshot_before_migration_and_no_composed_image_enumeration() -> None:
    text = RELEASE.read_text()
    assert text.index("- name: Stop application writers") < text.index(
        "- name: Create a second PostgreSQL backup"
    )
    assert text.index("- name: Snapshot Kafka") < text.index("- name: Apply Alembic migrations")
    assert text.index("- name: Snapshot Kafka") < text.index("- name: Remove only stopped")
    assert "community.docker.docker_compose_v2" not in text
    assert "down -v" not in text


def test_build_reuses_available_base_images_and_fetches_only_missing() -> None:
    """A GHCR TLS timeout must not be reintroduced by unconditional --pull."""
    text = RELEASE.read_text()
    assert text.index("- name: Inspect required application base images locally") < text.index(
        "- name: Build the application images"
    )
    assert "python:3.12-slim-bookworm" in text
    assert "ghcr.io/astral-sh/uv:0.12.1" in text
    assert "- name: Pull missing application base images with bounded retries" in text
    assert "when: item.rc != 0" in text
    assert "retries: 3" in text
    assert "sahabino_compose_cli + ['build'] + sahabino_application_services" in text
    assert "sahabino_compose_cli + ['build', '--pull']" not in text


def test_remote_branch_resolution_ignores_stale_local_branch(tmp_path: pathlib.Path) -> None:
    if not shutil.which("git") or not shutil.which("bash"):
        pytest.skip("Git/Bash unavailable")
    bare = tmp_path / "origin.git"
    repo = tmp_path / "working"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("config", "user.name", "test")
    git("config", "user.email", "test@example.org")
    git("remote", "add", "origin", str(bare))
    (repo / "VERSION").write_text("old")
    git("add", "VERSION")
    git("commit", "-m", "old")
    stale = git("rev-parse", "HEAD")
    git("push", "-u", "origin", "main")
    (repo / "VERSION").write_text("new")
    git("commit", "-am", "new")
    fresh = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    git("reset", "--hard", stale)
    bash = f"""
source '{ASSISTANT}'
APP_DIR='{repo}'
TARGET_REVISION=main
run_as_deploy_git() {{ git -C "$APP_DIR" "$@"; }}
step() {{ :; }}
info() {{ :; }}
resolve_revision
printf '%s\\n' "$TARGET_REVISION"
"""
    result = subprocess.run(["bash", "-c", bash], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == fresh
    assert stale != fresh


def test_persisted_container_never_removed_without_verified_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    runner_calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        runner_calls.append(argv)
        if argv[:3] == ["docker", "ps", "-aq"]:
            return response(argv, stdout="id\n" if argv[-1].endswith("=seaweedfs") else "")
        if argv[:4] == ["docker", "inspect", "--type", "container"]:
            return response(
                argv,
                stdout=json.dumps(
                    [
                        {
                            "Id": "id",
                            "Image": "sha256:gone",
                            "State": {"Running": True},
                            "Mounts": [{"Type": "volume", "Name": "sahabino_seaweedfs_data"}],
                        }
                    ]
                ),
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            return response(argv, code=1)
        raise AssertionError(f"No Docker mutation permitted yet: {argv}")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"project": "sahabino", "local_only": True, "volumes": {}}))
    with pytest.raises(RuntimeError, match="No verified backup"):
        containers.remove_stale_persisted("sahabino", manifest, runner=runner)
    assert not any(call[1] in ("stop", "rm") for call in runner_calls)


def test_persisted_container_checksum_mismatch_fails_before_stop(tmp_path: pathlib.Path) -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return response(argv, stdout="id\n" if argv[-1].endswith("=seaweedfs") else "")
            if argv[:4] == ["docker", "inspect", "--type", "container"]:
                return response(
                    argv,
                    stdout=json.dumps(
                        [
                            {
                                "Id": "id",
                                "Image": "sha256:gone",
                                "State": {"Running": True},
                                "Mounts": [{"Type": "volume", "Name": "sahabino_seaweedfs_data"}],
                            }
                        ]
                    ),
                )
            if argv[:3] == ["docker", "image", "inspect"]:
                return response(argv, code=1)
            raise AssertionError(f"Unexpected mutation: {argv}")

    (tmp_path / "seaweedfs.tar").write_bytes(b"tampered")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "project": "sahabino",
                "local_only": True,
                "volumes": {"seaweedfs": {"file": "seaweedfs.tar", "sha256": "0" * 64}},
            }
        )
    )
    runner = Runner()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        containers.remove_stale_persisted("sahabino", manifest, runner=runner)
    assert not any(call[1] in ("stop", "rm") for call in runner.calls)


def test_stop_app_only_uses_label_scoped_docker_stop_not_compose_images() -> None:
    class Runner(FakeDocker):
        def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["docker", "ps", "-aq"]:
                return super().__call__(argv, **kwargs)
            if argv[:4] == ["docker", "inspect", "--type", "container"]:
                return super().__call__(argv, **kwargs)
            if argv[:2] == ["docker", "stop"]:
                self.calls.append(argv)
                return response(argv, stdout="old-id")
            raise AssertionError(argv)

    runner = Runner(running=True)
    assert containers.stop_application("sahabino", runner=runner) == ["api"]
    assert ["docker", "stop", "--time", "60", "old-id"] in runner.calls


def test_bootstrap_runs_selected_revision_script_before_provision(tmp_path: pathlib.Path) -> None:
    if not shutil.which("git") or not shutil.which("bash"):
        pytest.skip("Git/Bash unavailable")
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("config", "user.name", "test")
    git("config", "user.email", "test@example.org")
    target = repo / "deploy/ansible/sahabino-deploy.sh"
    target.parent.mkdir(parents=True)
    target.write_text('#!/bin/bash\nprintf "STAGED:%s:%s\\n" "$SAHABINO_BOOTSTRAP_READY" "$*"\n')
    target.chmod(0o755)
    git("add", ".")
    git("commit", "-m", "tested release")
    sha = git("rev-parse", "HEAD")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    script = f"""
source '{ASSISTANT}'
APP_DIR='{repo}'
RUNTIME_DIR='{runtime}'
TARGET_REVISION='{sha}'
BOOTSTRAP_REVISION=
MODE=deploy
ORIGINAL_ARGS=(--deploy --revision main)
run_as_deploy_git() {{ git -C "$APP_DIR" "$@"; }}
info() {{ :; }}
bootstrap_selected_release
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert f"STAGED:{sha}:--deploy --revision main --revision {sha}" in result.stdout
    assert not list(runtime.iterdir())


def test_backup_failure_restarts_previous_persistent_containers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "important").write_text("kept")
    pg = tmp_path / "pg.dump"
    pg.write_bytes(b"fake verified by caller")
    key = tmp_path / "vault-key"
    key.write_text("test-key")
    key.chmod(0o600)
    events: list[list[str]] = []
    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        backup,
        "gather",
        lambda project, runner=None: [
            ("seaweedfs", volume, {"Id": "seaweed-id", "State": {"Running": True}})
        ],
    )
    monkeypatch.setattr(backup, "capacity", lambda *_args: (1024, 9999999999))

    def docker_event(argv: list[str], **_kwargs: object) -> str:
        events.append(argv)
        return "seaweed-id"

    monkeypatch.setattr(backup, "docker", docker_event)

    def corrupted_tar(_source: pathlib.Path, _destination: pathlib.Path) -> None:
        raise RuntimeError("snapshot write failed")

    monkeypatch.setattr(backup, "make_tar", corrupted_tar)
    with pytest.raises(RuntimeError, match="snapshot write failed"):
        backup.create_snapshot("sahabino", backup_root, app, pg, key, backup.MIN_MARGIN)
    assert events == [["stop", "--time", "60", "seaweed-id"], ["start", "seaweed-id"]]
    assert (volume / "important").read_text() == "kept"
    assert not list(backup_root.iterdir())


class OrphanDatabaseDocker:
    """Model real Docker responses without modifying a host Docker daemon."""

    def __init__(self, service: str, mount: pathlib.Path, *, project: str = "sahabino") -> None:
        self.service, self.mount, self.project = service, mount, project
        self.calls: list[list[str]] = []
        self.running = False
        self.image_present = True
        self.cluster = "MkU3OEVBNTcwNTJENDM2Qk"

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        name = f"{self.project}_{self.service}_data"
        if argv[:3] == ["docker", "ps", "-aq"]:
            if any(a == f"label=com.docker.compose.service={self.service}" for a in argv):
                return response(argv, stdout="recovered-id\n" if self.running else "")
            if any(a == f"volume={name}" for a in argv):
                return response(argv)
            return response(argv)
        if argv[:4] == ["docker", "inspect", "--type", "container"]:
            return response(
                argv,
                stdout=json.dumps(
                    [
                        {
                            "Id": "recovered-id",
                            "Image": "sha256:existing",
                            "State": {"Running": True},
                            "Mounts": [
                                {
                                    "Type": "volume",
                                    "Name": name,
                                    "Destination": containers.DATABASE_VOLUMES[self.service][1],
                                }
                            ],
                        }
                    ]
                ),
            )
        if argv[:3] == ["docker", "volume", "inspect"]:
            if argv[-1] != name:
                return response(argv, code=1, stderr="Error response from daemon: no such volume")
            return response(
                argv,
                stdout=json.dumps(
                    [
                        {
                            "Name": name,
                            "Driver": "local",
                            "Mountpoint": str(self.mount),
                            "Labels": {"com.docker.compose.project": self.project},
                        }
                    ]
                ),
            )
        if argv[:3] == ["docker", "compose", "--project-name"]:
            if argv[-3:] == ["config", "--format", "json"]:
                source, target = containers.DATABASE_VOLUMES[self.service]
                return response(
                    argv,
                    stdout=json.dumps(
                        {
                            "services": {
                                self.service: {
                                    "image": (
                                        "postgres:16-alpine"
                                        if self.service == "postgres"
                                        else "apache/kafka:4.3.1"
                                    ),
                                    "environment": {
                                        "POSTGRES_DB": "app",
                                        "POSTGRES_USER": "app",
                                        "POSTGRES_PASSWORD": "secret",
                                        "KAFKA_NODE_ID": "1",
                                        "CLUSTER_ID": self.cluster,
                                    },
                                    "volumes": [
                                        {"type": "volume", "source": source, "target": target}
                                    ],
                                }
                            },
                            "volumes": {source: {"name": name}},
                        }
                    ),
                )
            if "up" in argv:
                self.running = True
                return response(argv, stdout="Started\n")
            if "pull" in argv:
                self.image_present = True
                return response(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return response(argv, code=0 if self.image_present else 1)
        raise AssertionError(f"Unexpected Docker call: {argv}")


def make_orphan_volume(root: pathlib.Path, service: str) -> pathlib.Path:
    root.mkdir()
    (root / "existing-data-sentinel").write_bytes(b"NEVER LOSE ME")
    if service == "postgres":
        (root / "PG_VERSION").write_text("16\n")
        (root / "global").mkdir()
        (root / "global/pg_control").write_bytes(b"existing-cluster-control")
        (root / "base").mkdir()
        (root / "pg_wal").mkdir()
    else:
        (root / "meta.properties").write_text("cluster.id=MkU3OEVBNTcwNTJENDM2Qk\nnode.id=1\n")
    return root


@pytest.mark.parametrize("service", ["postgres", "kafka"])
def test_orphan_database_recovery_preserves_old_volume_and_backs_up_first(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    mount = make_orphan_volume(tmp_path / "volume", service)
    runner = OrphanDatabaseDocker(service, mount)
    monkeypatch.setattr(containers.os, "geteuid", lambda: 0)
    result = containers.recover_missing_database(
        "sahabino",
        service,
        ["docker", "compose", "--project-name", "sahabino"],
        tmp_path / "releases",
        runner=runner,
    )
    assert result.startswith(f"RECOVERED:{service}:")
    manifest = pathlib.Path(result.split(":", 2)[2])
    meta = json.loads(manifest.read_text())
    archive = manifest.parent / meta["archive"]
    assert containers.hashlib.sha256(archive.read_bytes()).hexdigest() == meta["sha256"]
    with tarfile.open(archive) as tar:
        assert tar.extractfile("data/existing-data-sentinel").read() == b"NEVER LOSE ME"
    assert (mount / "existing-data-sentinel").read_bytes() == b"NEVER LOSE ME"
    assert runner.running
    assert not any("rm" in cmd or "down" in cmd or "-v" in cmd for cmd in runner.calls)
    snapshot = next(i for i, cmd in enumerate(runner.calls) if "image" in cmd)
    up = next(i for i, cmd in enumerate(runner.calls) if "up" in cmd)
    assert snapshot < up


def test_orphan_kafka_cluster_mismatch_does_not_start_container(tmp_path: pathlib.Path) -> None:
    mount = make_orphan_volume(tmp_path / "volume", "kafka")
    (mount / "meta.properties").write_text("cluster.id=wrong\nnode.id=1\n")
    runner = OrphanDatabaseDocker("kafka", mount)
    with pytest.raises(RuntimeError, match="cluster/node identity"):
        containers.recover_missing_database(
            "sahabino",
            "kafka",
            ["docker", "compose", "--project-name", "sahabino"],
            tmp_path / "backup",
            runner=runner,
        )
    assert not runner.running
    assert not (tmp_path / "backup").exists()


def test_orphan_postgres_wrong_major_does_not_start(tmp_path: pathlib.Path) -> None:
    mount = make_orphan_volume(tmp_path / "volume", "postgres")
    (mount / "PG_VERSION").write_text("15\n")
    runner = OrphanDatabaseDocker("postgres", mount)
    with pytest.raises(RuntimeError, match="major version"):
        containers.recover_missing_database(
            "sahabino",
            "postgres",
            ["docker", "compose", "--project-name", "sahabino"],
            tmp_path / "backup",
            runner=runner,
        )
    assert not runner.running


def test_orphan_recovery_insufficient_space_stops_before_compose_up(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = make_orphan_volume(tmp_path / "volume", "postgres")
    runner = OrphanDatabaseDocker("postgres", mount)
    DiskUsage = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(containers.os, "geteuid", lambda: 0)
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda _: DiskUsage(1000, 999, 1))
    with pytest.raises(RuntimeError, match="Insufficient disk"):
        containers.recover_missing_database(
            "sahabino",
            "postgres",
            ["docker", "compose", "--project-name", "sahabino"],
            tmp_path / "backup",
            runner=runner,
        )
    assert not runner.running
    assert (mount / "existing-data-sentinel").read_bytes() == b"NEVER LOSE ME"


def test_orphan_recovery_rejects_missing_volume_when_partner_survives(
    tmp_path: pathlib.Path,
) -> None:
    mount = make_orphan_volume(tmp_path / "volume", "kafka")
    runner = OrphanDatabaseDocker("kafka", mount)
    with pytest.raises(RuntimeError, match="refusing new empty state"):
        containers.recover_missing_database(
            "sahabino",
            "postgres",
            ["docker", "compose", "--project-name", "sahabino"],
            tmp_path / "backup",
            runner=runner,
        )
    assert not runner.running


def test_script_recovers_both_databases_before_same_revision_skip_and_pg_dump() -> None:
    text = ASSISTANT.read_text()
    start = text.index("precheckout_backup_and_checkout() {")
    end = text.index("\nrun_ansible_syntax_check()", start)
    body = text[start:end]
    assert "for recovery_service in postgres kafka" in body
    assert body.index("recover-missing") < body.index('[[ "$current" == "$TARGET_REVISION" ]]')
    assert body.index("recover-missing") < body.index("sahabino-postgres-backup pre-deploy")
    assert '( cd "$APP_DIR" &&' in body
    assert "compose down -v" not in body


def test_recovery_cli_parses_flags_after_mode_and_preserves_compose_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Regression: argparse.REMAINDER used to swallow --project after mode."""
    seen: list[tuple[str, str, list[str], pathlib.Path]] = []
    compose = ["docker", "compose", "--project-name", "sahabino", "-f", "compose.prod.yml"]

    def recover(project: str, service: str, argv: list[str], root: pathlib.Path) -> str:
        seen.append((project, service, argv, root))
        return "EXISTING:postgres"

    monkeypatch.setattr(containers, "recover_missing_database", recover)
    monkeypatch.setattr(
        containers.sys,
        "argv",
        [
            "release_containers.py",
            "recover-missing",
            "--project",
            "sahabino",
            "--service",
            "postgres",
            "--backup-root",
            str(tmp_path),
            "--",
            *compose,
        ],
    )
    assert containers.main() == 0
    assert seen == [("sahabino", "postgres", compose, tmp_path)]


def test_ensure_cli_parses_flags_after_mode_and_preserves_compose_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, list[str]]] = []
    compose = ["docker", "compose", "--project-name", "sahabino"]
    monkeypatch.setattr(
        containers,
        "ensure_infrastructure",
        lambda project, service, argv: seen.append((project, service, argv)),
    )
    monkeypatch.setattr(
        containers.sys,
        "argv",
        [
            "release_containers.py",
            "ensure",
            "--project",
            "sahabino",
            "--service",
            "kafka",
            "--",
            *compose,
        ],
    )
    assert containers.main() == 0
    assert seen == [("sahabino", "kafka", compose)]


def test_stop_app_cli_parses_project_after_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(containers, "stop_application", lambda project: seen.append(project) or [])
    monkeypatch.setattr(
        containers.sys, "argv", ["release_containers.py", "stop-app", "--project", "sahabino"]
    )
    assert containers.main() == 0
    assert seen == ["sahabino"]


@pytest.mark.parametrize("service", ["postgres", "kafka", "seaweedfs"])
def test_persisted_container_requires_original_data_mount_before_start(service: str) -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return response(argv, stdout="existing-id\n")
            if argv[:4] == ["docker", "inspect", "--type", "container"]:
                return response(
                    argv,
                    stdout=json.dumps(
                        [
                            {
                                "Id": "existing-id",
                                "State": {"Running": False},
                                "Mounts": [
                                    {
                                        "Type": "volume",
                                        "Name": "incorrect-volume",
                                        "Destination": "/data",
                                    }
                                ],
                            }
                        ]
                    ),
                )
            raise AssertionError(f"No mutations or additional Docker calls should occur: {argv}")

    runner = Runner()
    with pytest.raises(RuntimeError, match="Unsafe.*data mount"):
        containers.ensure_infrastructure(
            "sahabino",
            service,
            ["docker", "compose", "--project-name", "sahabino"],
            runner=runner,
        )
    assert not any(argv[1] in ("start", "rm", "volume") or "up" in argv for argv in runner.calls)


def test_restored_api_is_healthy_before_workers_and_crawler() -> None:
    text = (ROOT / "deploy/ansible/roles/sahabino_restore/tasks/main.yml").read_text()
    assert (
        text.index("- name: Start the API using")
        < text.index("- name: Verify API health after restore before")
        < text.index("- name: Start ingestion and analyzer")
        < text.index("- name: Start crawler only after")
    )
    assert "use_proxy: false" in text


def test_api_self_health_ignores_injected_http_proxy() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "urllib.request.ProxyHandler({})" in compose
    assert "http://127.0.0.1:8000/docs" in compose
