from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, Undefined

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = ROOT / "infrastructure/observability/grafana/dashboards"
DASHBOARDS = {
    "sahabino-overview.json": "sahabino-overview",
    "sahabino-crawler.json": "sahabino-crawler",
    "sahabino-reviews.json": "sahabino-reviews",
    "sahabino-application-explorer.json": "sahabino-application-explorer",
}
POSTGRES_UID = "sahabino-postgres"


def _dashboards() -> list[dict[str, object]]:
    return [json.loads((DASHBOARD_DIR / name).read_text()) for name in DASHBOARDS]


def test_four_postgres_dashboards_are_valid_git_managed_json() -> None:
    dashboards = _dashboards()

    assert [dashboard["uid"] for dashboard in dashboards] == list(DASHBOARDS.values())
    assert sum(len(dashboard["panels"]) for dashboard in dashboards) >= 36
    assert (DASHBOARD_DIR / "sahabino-logging-smoke.json").is_file()

    for dashboard in dashboards:
        assert dashboard["refresh"] not in {"5s", "10s", "30s"}
        assert dashboard["editable"] is True
        for panel in dashboard["panels"]:
            assert panel["title"]
            assert panel["description"]
            assert panel["datasource"]["uid"] == POSTGRES_UID
            assert panel["targets"]
            for target in panel["targets"]:
                assert target["datasource"]["uid"] == POSTGRES_UID
                assert target["rawSql"].strip()


def test_dashboard_sql_excludes_sensitive_text_and_uses_bounded_tables() -> None:
    forbidden = re.compile(r"\b(content|author_name|error_message|external_review_id)\b", re.I)

    for dashboard in _dashboards():
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                sql = target["rawSql"]
                assert forbidden.search(sql) is None
                if "ORDER BY" in sql and panel["type"] == "table":
                    assert "LIMIT" in sql or "GROUP BY" in sql


def test_application_variable_is_single_value_and_sql_string_escaped() -> None:
    dashboard = json.loads((DASHBOARD_DIR / "sahabino-application-explorer.json").read_text())
    variable = dashboard["templating"]["list"][0]

    assert variable["name"] == "application_id"
    assert variable["multi"] is False
    assert variable["includeAll"] is False
    assert "id::text AS __value" in variable["query"]
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            sql = target["rawSql"]
            assert "NULLIF(${application_id:sqlstring}, '')::uuid" in sql
            assert "$application_id" not in sql


def test_postgres_datasource_is_optional_secure_and_matches_postgres_16() -> None:
    datasource = (
        ROOT / "infrastructure/observability/grafana/provisioning/datasources/postgres.yaml"
    ).read_text()
    entrypoint = (ROOT / "infrastructure/observability/grafana/entrypoint.sh").read_text()
    loki = (
        ROOT / "infrastructure/observability/grafana/provisioning/datasources/loki.yaml"
    ).read_text()
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "uid: sahabino-postgres" in datasource
    assert "url: postgres:5432" in datasource
    assert "user: grafana_reader" in datasource
    assert "postgresVersion: 1600" in datasource
    assert "sslmode: disable" in datasource
    assert "secureJsonData:" in datasource
    assert "password: $GRAFANA_POSTGRES_PASSWORD" in datasource
    assert "${GRAFANA_POSTGRES_PASSWORD}" not in datasource
    assert "prune: true" in datasource
    assert 'if [ -z "${GRAFANA_POSTGRES_PASSWORD:-}" ]' in entrypoint
    assert 'rm -f "$runtime_dir/datasources/postgres.yaml"' in entrypoint
    assert "POSTGRES_DB: ${POSTGRES_DB:-sahabino}" in compose
    assert "GRAFANA_POSTGRES_PASSWORD: ${GRAFANA_POSTGRES_PASSWORD:-}" in compose
    assert "grafana_data:/var/lib/grafana" in compose
    assert "uid: sahabino-loki" in loki
    assert "isDefault: true" in loki


def test_reader_contract_is_column_restricted_and_never_a_required_secret() -> None:
    sql = (ROOT / "deploy/ansible/roles/sahabino_deploy/files/grafana_reader.sql").read_text()
    validator = (ROOT / "deploy/ansible/tools/validate_production_env.py").read_text()
    preflight = (ROOT / "deploy/ansible/roles/sahabino_deploy/tasks/preflight.yml").read_text()

    assert "NOSUPERUSER" in sql
    assert "NOCREATEDB" in sql
    assert "NOCREATEROLE" in sql
    assert "CONNECTION LIMIT 5" in sql
    assert "default_transaction_read_only = on" in sql
    assert "statement_timeout = '30s'" in sql
    assert "GRANT SELECT (" in sql
    assert "GRANT SELECT ON" not in sql
    assert "author_name" not in sql
    assert "content" not in sql
    assert "error_message" not in sql
    assert "GRAFANA_POSTGRES_PASSWORD" not in validator
    assert "vault_sahabino_grafana_reader_password is defined" not in preflight


def test_optional_reader_credential_is_validated_before_environment_rendering() -> None:
    template = (
        ROOT / "deploy/ansible/roles/sahabino_deploy/templates/production.env.j2"
    ).read_text()
    task = (ROOT / "deploy/ansible/roles/sahabino_deploy/tasks/grafana_postgres.yml").read_text()

    assert "is match('^[A-Za-z0-9_.@$-]{20,}$')" in template
    assert "'CHANGE_ME' not in" in template
    assert "sahabino_grafana_postgres_credential_valid" in task
    assert "does not meet the documented safe format" in task
    assert "sahabino_grafana_reader_rotate_password | default(false) | bool" in task


def test_optional_reader_environment_omits_invalid_or_placeholder_credentials() -> None:
    source = (ROOT / "deploy/ansible/roles/sahabino_deploy/templates/production.env.j2").read_text()
    environment = Environment()
    environment.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    environment.filters["bool"] = bool
    environment.filters["to_json"] = lambda value: json.dumps(
        None if isinstance(value, Undefined) else value
    )
    template = environment.from_string(source)

    def render(password: str) -> str:
        return template.render(vault_sahabino_grafana_reader_password=password)

    assert "GRAFANA_POSTGRES_PASSWORD" not in render("too-short")
    assert "GRAFANA_POSTGRES_PASSWORD" not in render("CHANGE_ME_USE_A_LONG_PASSWORD")
    assert "GRAFANA_POSTGRES_PASSWORD='Primary$Password-2026'" in render("Primary$Password-2026")


def test_optional_vault_reference_contract_uses_the_existing_postgres_secret() -> None:
    vault_example = (ROOT / "deploy/ansible/group_vars/production/vault.yml.example").read_text()
    shared_password = "PrimaryPassword-2026"
    reference = "{{ vault_sahabino_postgres_password }}"

    assert reference in vault_example
    assert (
        Environment()
        .from_string(reference)
        .render(vault_sahabino_postgres_password=shared_password)
        == shared_password
    )


def test_ansible_resolves_optional_vault_reference_when_available(tmp_path: Path) -> None:
    ansible_playbook = shutil.which("ansible-playbook")
    if ansible_playbook is None:
        pytest.skip("ansible-playbook is unavailable")

    playbook = tmp_path / "vault-reference.yml"
    playbook.write_text(
        """
---
- hosts: localhost
  gather_facts: false
  vars:
    vault_sahabino_postgres_password: PrimaryPassword-2026
    vault_sahabino_grafana_reader_password: "{{ vault_sahabino_postgres_password }}"
  tasks:
    - name: Resolve the optional reference without printing either secret
      ansible.builtin.assert:
        that:
          - vault_sahabino_grafana_reader_password == vault_sahabino_postgres_password
      no_log: true
""".lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [ansible_playbook, "--inventory", "localhost,", "--connection", "local", str(playbook)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, "Ansible did not resolve the optional Vault reference."


def test_optional_reader_runs_after_migrations_and_before_service_reconciliation() -> None:
    release = (ROOT / "deploy/ansible/roles/sahabino_deploy/tasks/release.yml").read_text()
    task = (ROOT / "deploy/ansible/roles/sahabino_deploy/tasks/grafana_postgres.yml").read_text()

    assert (
        release.index("Verify the database is at the checked-out Alembic head")
        < release.index("Configure the optional least-privilege Grafana PostgreSQL reader")
        < release.index("Provision and verify the required Kafka topic topology")
    )
    assert "rescue:" in task
    assert "state: absent" in task
    assert "ignore_errors" not in task
    assert "failed_when: false" not in task
    assert "no_log: true" in task
