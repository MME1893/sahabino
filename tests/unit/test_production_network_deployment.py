from pathlib import Path

ROOT = Path(__file__).parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_production_enables_existing_and_network_profiles() -> None:
    variables = read("deploy/ansible/group_vars/production/vars.yml")
    defaults = read("deploy/ansible/roles/sahabino_deploy/defaults/main.yml")
    for profile in ("observability", "network"):
        assert f"  - {profile}" in variables
        assert f"  - {profile}" in defaults


def test_expected_service_set_is_additive() -> None:
    variables = read("deploy/ansible/group_vars/production/vars.yml")
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


def test_runtime_secret_and_storage_init_contract() -> None:
    compose = read("docker-compose.yml")
    configure = read("deploy/ansible/roles/sahabino_deploy/tasks/configure.yml")
    release = read("deploy/ansible/roles/sahabino_deploy/tasks/release.yml")
    assert "SAHABINO_SEAWEEDFS_S3_CONFIG_PATH" in compose
    assert 'mode: "0600"' in configure
    assert "json.tool" in configure
    assert "python', '-m', 'sahabino.network', 'storage-init'" in release
    assert release.index("Start SeaweedFS before") < release.index("Stop database-writing")


def test_assistant_safe_exposure_and_drain_contract() -> None:
    assistant = read("deploy/ansible/sahabino-deploy.sh")
    release = read("deploy/ansible/roles/sahabino_deploy/tasks/release.yml")
    assert "COMPOSE_PROFILES=(observability network)" in assistant
    assert "if (( ASSUME_YES )); then OBJECT_STORAGE_EXPOSURE=private" in assistant
    assert "--confirm-public-object-storage" in assistant
    assert "--allow-active-crawl-interruption" in assistant
    assert "WHERE status IN ('pending','running')" in release
    assert "Perform final persisted crawl-state race check" in release


def test_historical_migration_remains_at_expected_revision() -> None:
    migration = read("migrations/versions/20260913_0005_seed_required_applications.py")
    assert 'revision: str = "20260913_0005"' in migration
