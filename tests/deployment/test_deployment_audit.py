"""Cross-file deployment audits: syntax, CLI boundaries, runtime shell and safety gates.

These checks need no Docker daemon or credentials and run on every pull request.
They are not a replacement for a production-like Docker integration test.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import jinja2
import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

ROOT = pathlib.Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
VERIFY = ANSIBLE / "roles/sahabino_deploy/tasks/verify.yml"
RELEASE = ANSIBLE / "roles/sahabino_deploy/tasks/release.yml"
RESTORE = ANSIBLE / "roles/sahabino_restore/tasks/main.yml"
LAUNCHER = ANSIBLE / "sahabino-deploy.sh"
ENTRYPOINT = ROOT / "infrastructure/network/seaweedfs-entrypoint.sh"


class ComposeOverrideLoader(yaml.SafeLoader):
    pass


def _override(loader: ComposeOverrideLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


ComposeOverrideLoader.add_constructor("!override", _override)


def _walk(value: object) -> list[str]:
    if isinstance(value, dict):
        return [leaf for sub in value.values() for leaf in _walk(sub)]
    if isinstance(value, list):
        return [leaf for sub in value for leaf in _walk(sub)]
    return [value] if isinstance(value, str) else []


def test_every_ansible_yaml_and_jinja_expression_parses() -> None:
    """Catch template syntax mistakes in nested Ansible declarations before VPS."""
    environment = jinja2.Environment()
    templates = 0
    files = list(ANSIBLE.rglob("*.yml")) + list(ANSIBLE.rglob("*.yaml"))
    assert len(files) >= 20
    for path in files:
        obj = yaml.safe_load(path.read_text())
        for text in _walk(obj):
            if "{{" in text or "{%" in text:
                environment.parse(text)
                templates += 1
    assert templates >= 250


def test_compose_yaml_parses_with_override_and_has_preserved_volumes() -> None:
    base = yaml.load((ROOT / "docker-compose.yml").read_text(), Loader=ComposeOverrideLoader)
    prod = yaml.load((ROOT / "compose.prod.yml").read_text(), Loader=ComposeOverrideLoader)
    expected = {
        "postgres_data",
        "kafka_data",
        "seaweedfs_data",
        "grafana_data",
        "loki_data",
        "alloy_data",
    }
    assert expected <= set(base["volumes"])
    assert expected <= set(prod.get("volumes", base["volumes"]))
    assert "seaweedfs_data:/data" in prod["services"]["seaweedfs"]["volumes"]
    assert any(
        "seaweedfs-entrypoint.sh:" in str(v) for v in prod["services"]["seaweedfs"]["volumes"]
    )


def test_seaweed_runtime_audit_is_one_shared_script_and_rendered_argv_is_safe() -> None:
    text = VERIFY.read_text()
    assert "awk" not in text and "pid1_uid=" not in text
    assert "--verify-runtime" in RELEASE.read_text()
    assert "--verify-runtime" in LAUNCHER.read_text()
    assert "--verify-runtime" in ENTRYPOINT.read_text()
    tasks = yaml.safe_load(text)
    matching = [
        t
        for t in tasks
        if t.get("name") == "Verify the staged SeaweedFS credential and exact PID 1 identity"
    ]
    assert len(matching) == 1
    template = matching[0]["ansible.builtin.command"]["argv"]
    rendered = (
        NativeEnvironment()
        .from_string(template)
        .render(
            sahabino_compose_cli=["docker", "compose", "--project-name", "sahabino"],
        )
    )
    assert isinstance(rendered, list)
    assert rendered[-9:] == [
        "exec",
        "-T",
        "--user",
        "0",
        "seaweedfs",
        "sh",
        "-s",
        "--",
        "--verify-runtime",
    ]
    assert all("/usr/local/bin/sahabino-seaweedfs-entrypoint" not in item for item in rendered)
    assert all("$2" not in item for item in rendered)
    assert (
        "lookup('ansible.builtin.file', sahabino_app_dir + "
        "'/infrastructure/network/seaweedfs-entrypoint.sh')"
        in matching[0]["ansible.builtin.command"]["stdin"]
    )
    assert matching[0]["ansible.builtin.command"]["stdin_add_newline"] is False


def test_stale_seaweedfs_single_file_mount_cannot_hijack_verifier(tmp_path: pathlib.Path) -> None:
    """Reproduce the production failure: old mounted entrypoint forwards flags to weed."""
    old_bound = tmp_path / "old-entrypoint.sh"
    old_bound.write_text('#!/bin/sh\nprintf "OLD_MOUNT_FORWARDED:%s\n" "$1"\n')
    old = subprocess.run(["sh", str(old_bound), "--verify-runtime"], capture_output=True, text=True)
    assert old.stdout.strip() == "OLD_MOUNT_FORWARDED:--verify-runtime"
    # A stdin-fed script is the checked-out version, independent of the old
    # bind mount. Verify POSIX sh preserves the --verify-runtime argument.
    probe = subprocess.run(
        ["sh", "-s", "--", "--verify-runtime"],
        input='[ "$1" = "--verify-runtime" ] && printf "FRESH_VERIFIER\n"\n',
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "FRESH_VERIFIER"
    release_tasks = yaml.safe_load(RELEASE.read_text())
    for task in (release_tasks, yaml.safe_load(VERIFY.read_text())):
        matches = [
            v
            for v in task
            if v.get("name")
            in (
                "Verify SeaweedFS runtime before entering maintenance",
                "Verify the staged SeaweedFS credential and exact PID 1 identity",
            )
        ]
        assert len(matches) == 1
        command = matches[0]["ansible.builtin.command"]
        assert "'/usr/local/bin/sahabino-seaweedfs-entrypoint'" not in command["argv"]
        assert command["stdin_add_newline"] is False
        assert "ansible.builtin.file" in command["stdin"]


@pytest.mark.parametrize(
    "script",
    sorted((ROOT / "deploy").rglob("*.sh")) + sorted((ROOT / "infrastructure").rglob("*.sh")),
)
def test_every_deployment_shell_script_parses(script: pathlib.Path) -> None:
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    if script == ENTRYPOINT:
        assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0


@pytest.mark.parametrize(
    "name", ["crawler_drain.py", "release_containers.py", "kafka_group_health.py"]
)
def test_helper_cli_help_and_no_greedy_remainder(name: str) -> None:
    text = (ANSIBLE / "tools" / name).read_text()
    assert not re.search(r"nargs\s*=\s*argparse\.REMAINDER", text)
    result = subprocess.run(
        [sys.executable, str(ANSIBLE / "tools" / name), "--help"], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_fail_safe_and_marker_are_ordered_after_both_verification_stages() -> None:
    deploy = (ANSIBLE / "deploy.yml").read_text()
    shell = LAUNCHER.read_text()
    assert "when: sahabino_deployment_writer_stop_started | default(false) | bool" in deploy
    assert "No database restore or" in deploy and "image rollback was performed" in deploy
    assert "DEPLOYED_REVISION" not in VERIFY.read_text()
    main = shell[shell.index("main() {") :]
    assert (
        main.rindex("  run_deploy\n")
        < main.rindex("  verify_deployment\n")
        < main.rindex("  finalize_deployment_revision\n")
    )
    assert "git reset --hard" not in deploy
    assert "docker volume prune" not in deploy


def test_final_marker_refuses_sha_mismatch_without_replacing_old_value(
    tmp_path: pathlib.Path,
) -> None:
    current = "a" * 40
    incoming = "b" * 40
    marker = tmp_path / "DEPLOYED_REVISION"
    marker.write_text(current + "\n")
    script = f'''
source "{LAUNCHER}"
APP_PARENT="{tmp_path}"
TARGET_REVISION="{incoming}"
run_as_deploy_git() {{ printf '%s\\n' "{current}"; }}
if finalize_deployment_revision; then exit 9; fi
cat "{marker}"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == current
    assert marker.read_text().strip() == current


def test_restore_and_release_wait_for_api_before_workers() -> None:
    for path in (RESTORE, RELEASE):
        text = path.read_text()
        if path == RESTORE:
            assert (
                text.index("- name: Start the API using")
                < text.index("- name: Verify API health after restore before")
                < text.index("- name: Start ingestion and analyzer")
                < text.index("- name: Start crawler only after")
            )
            assert "use_proxy: false" in text
        else:
            assert (
                text.index("- name: Start the newly built API")
                < text.index("- name: Recreate ingestion and network analyzer")
                < text.index("- name: Recreate the crawler")
            )


def test_predowntime_guards_precede_writer_stop() -> None:
    release = RELEASE.read_text()
    assert release.index("- name: Check snapshot capacity before downloading") < release.index(
        "- name: Build the application images"
    )
    assert release.index(
        "- name: Verify SeaweedFS runtime before entering maintenance"
    ) < release.index("- name: Stop application writers")
    assert release.index("- name: Wait for persisted crawler work to drain") < release.index(
        "- name: Stop application writers"
    )
    assert release.index("- name: Snapshot Kafka, SeaweedFS") < release.index(
        "- name: Apply Alembic migrations"
    )
