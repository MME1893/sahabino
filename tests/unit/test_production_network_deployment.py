from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ASSISTANT = ROOT / "deploy/ansible/sahabino-deploy.sh"


def read(path: str) -> str:
    return (ROOT / path).read_text()


def run_assistant_function(body: str, *, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{ASSISTANT}"; {body}'],
        input=stdin,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )


def test_production_service_model_is_additive() -> None:
    variables = read("deploy/ansible/group_vars/production/vars.yml")
    for profile in ("observability", "network"):
        assert f"  - {profile}" in variables
    for service in (
        "postgres",
        "kafka",
        "seaweedfs",
        "api",
        "crawler",
        "ingestion",
        "network-analyzer",
        "loki",
        "alloy",
        "grafana",
    ):
        assert f"  - {service}" in variables


def test_central_compose_function_passes_both_profiles_exactly(tmp_path: Path) -> None:
    result = run_assistant_function(
        f"""APP_DIR={tmp_path!s}; docker() {{ printf '%q ' "$@"; printf '\\n'; }}; export -f docker;
        for command in "config --quiet" "ps" "logs api" "exec -T api true" "ps -q"; do compose $command; done"""  # noqa: E501
    )
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        assert line.count("--profile observability") == 1
        assert line.count("--profile network") == 1
        assert "--profile observability --profile network" in line


def test_interactive_private_default() -> None:
    result = run_assistant_function(
        'ASSUME_YES=0; OBJECT_STORAGE_EXPOSURE=""; select_object_storage_exposure; '
        'printf "%s|%s|%s" "$OBJECT_STORAGE_EXPOSURE" "$OBJECT_STORAGE_BIND_ADDRESS" "$OBJECT_STORAGE_PUBLIC_ENDPOINT"',  # noqa: E501
        stdin="\n",
    )
    assert result.returncode == 0
    assert result.stdout.endswith("private|127.0.0.1|http://127.0.0.1:8333")


def test_interactive_public_prompts_for_endpoint_and_acknowledgement() -> None:
    result = run_assistant_function(
        'ASSUME_YES=0; OBJECT_STORAGE_EXPOSURE=""; OBJECT_STORAGE_PUBLIC_ENDPOINT=""; '
        "CONFIRM_PUBLIC_OBJECT_STORAGE=0; select_object_storage_exposure; "
        'printf "%s|%s|%s" "$OBJECT_STORAGE_EXPOSURE" "$OBJECT_STORAGE_BIND_ADDRESS" "$OBJECT_STORAGE_PUBLIC_ENDPOINT"',  # noqa: E501
        stdin="2\nhttps://objects.example.test:8333\ny\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("public|0.0.0.0|https://objects.example.test:8333")
    assert "does not manage the firewall" in result.stderr


@pytest.mark.parametrize("endpoint", ["seaweedfs:8333", "http://bad/path", "ftp://example.test"])
def test_malformed_public_endpoint_is_rejected(endpoint: str) -> None:
    result = run_assistant_function(
        f"ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=public; OBJECT_STORAGE_PUBLIC_ENDPOINT={endpoint!r}; "  # noqa: E501
        "CONFIRM_PUBLIC_OBJECT_STORAGE=1; select_object_storage_exposure"
    )
    assert result.returncode != 0
    assert "requires an externally usable" in result.stderr


def test_yes_defaults_private_and_public_requires_acknowledgement() -> None:
    private = run_assistant_function(
        'ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=""; select_object_storage_exposure; echo "$OBJECT_STORAGE_EXPOSURE"'  # noqa: E501
    )
    assert private.returncode == 0 and private.stdout.strip() == "private"
    public = run_assistant_function(
        "ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=public; OBJECT_STORAGE_PUBLIC_ENDPOINT=https://objects.example.test; "  # noqa: E501
        "CONFIRM_PUBLIC_OBJECT_STORAGE=0; select_object_storage_exposure"
    )
    assert public.returncode != 0
    assert "--confirm-public-object-storage" in public.stderr


def test_public_access_hints_keep_observability_and_api(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SAHABINO_OBJECT_STORAGE_EXPOSURE=public\n")
    result = run_assistant_function(f"APP_DIR={tmp_path!s}; print_access_hints")
    assert result.returncode == 0
    for text in ("Grafana:", "Loki:", "Alloy:", "API exposure currently configured"):
        assert text in result.stdout
    assert "no SSH forward is required" in result.stdout


def test_runtime_secret_host_access_contract() -> None:
    if os.geteuid() != 0 or shutil.which("setpriv") is None:
        pytest.skip("root and setpriv are required for isolated UID/GID access test")
    test_root = Path(tempfile.mkdtemp(prefix="sahabino-secret-", dir="/tmp"))
    test_root.chmod(0o711)
    runtime = test_root / "runtime"
    runtime.mkdir(mode=0o710)
    secret = runtime / "seaweedfs-s3.json"
    secret.write_text('{"identities": []}')
    os.chown(runtime, 0, 1999)
    os.chown(secret, 0, 1999)
    secret.chmod(0o640)
    allowed = subprocess.run(
        ["setpriv", "--reuid=2000", "--regid=2000", "--groups=1999", "test", "-r", str(secret)],
        check=False,
    )
    unrelated = subprocess.run(
        ["setpriv", "--reuid=2001", "--regid=2001", "--clear-groups", "test", "-r", str(secret)],
        check=False,
    )
    assert allowed.returncode == 0
    assert unrelated.returncode != 0
    assert secret.stat().st_mode & 0o007 == 0


def docker_available() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
    )


@pytest.mark.skipif(not docker_available(), reason="Docker daemon unavailable")
def test_pinned_seaweedfs_runtime_identity_exists_and_is_non_root() -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "chrislusf/seaweedfs:4.46",
            "-c",
            "id seaweed",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "uid=0(" not in result.stdout
    assert "seaweed" in result.stdout


def test_crawler_drain_has_bounded_prompt_and_final_race_guard() -> None:
    release = read("deploy/ansible/roles/sahabino_deploy/tasks/release.yml")
    assert "retries:" in release and "delay:" in release
    assert "Abort deployment (default/recommended)" in release
    assert "user_input | default('') == '2'" in release
    assert release.index("Perform final persisted crawl-state race check") < release.index(
        "Stop database-writing"
    )


def test_analyzer_verification_requires_active_group_membership() -> None:
    verification = read("deploy/ansible/roles/sahabino_deploy/tasks/verify.yml")
    assert "--members" in verification
    assert "'CONSUMER-ID' in sahabino_network_group.stdout" in verification
    assert "does not exist" not in verification


def test_historical_migration_remains_at_expected_revision() -> None:
    migration = read("migrations/versions/20260913_0005_seed_required_applications.py")
    assert 'revision: str = "20260913_0005"' in migration
