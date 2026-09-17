# ruff: noqa: E501
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = ROOT / "deploy" / "ansible" / "tools"
ASSISTANT = ROOT / "deploy" / "ansible" / "sahabino-deploy.sh"
SEAWEED_WRAPPER = ROOT / "infrastructure" / "network" / "seaweedfs-entrypoint.sh"
RELEASE_TASKS = ROOT / "deploy" / "ansible" / "roles" / "sahabino_deploy" / "tasks" / "release.yml"
VERIFY_TASKS = ROOT / "deploy" / "ansible" / "roles" / "sahabino_deploy" / "tasks" / "verify.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "deployment-safety.yml"


def _load(name: str) -> ModuleType:
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


public_origin = _load("validate_public_origin")
environment_validator = _load("validate_production_env")
atomic_config = _load("atomic_seaweedfs_config")
crawler_drain = _load("crawler_drain")
kafka_health = _load("kafka_group_health")


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com:abc",
        "http://:8333",
        "https://user:pass@example.com",
        "https://example.com?x=1",
        "https://example.com#fragment",
        "https://example.com:99999",
        "http://127.0.0.1:8333",
        "http://localhost:8333",
        "http://0.0.0.0:8333",
        "http://storage.internal:8333",
        "http://single-label:8333",
    ],
)
def test_public_origin_rejects_unsafe_values(origin: str) -> None:
    with pytest.raises(ValueError):
        public_origin.validate_public_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "https://storage.example.org",
        "https://storage.example.org/",
        "http://storage.example.org:8333",
        "https://[2606:4700:4700::1111]:8333",
    ],
)
def test_public_origin_accepts_external_origins(origin: str) -> None:
    public_origin.validate_public_origin(origin)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "GROUP COORDINATOR (ID) ASSIGNMENT-STRATEGY STATE #MEMBERS\nignored values Stable 1\n",
            ("Stable", 1),
        ),
        ("anything Stable 3\n", ("Stable", 3)),
        ("anything PreparingRebalance 1\n", ("PreparingRebalance", 1)),
        ("anything Stable 0\n", ("Stable", 0)),
        ("Consumer group does not exist.\n", None),
    ],
)
def test_kafka_43_state_parser_uses_state_and_member_columns(
    output: str, expected: tuple[str, int] | None
) -> None:
    assert kafka_health.parse_group_state(output) == expected


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fake"], returncode, stdout, stderr)


def test_kafka_health_accepts_live_consumer_without_offsets() -> None:
    assert kafka_health.wait_for_healthy_group(
        ["fake"],
        timeout_seconds=0,
        interval_seconds=0,
        runner=lambda _: _completed(0, "row Stable 1\n"),
    )


def test_kafka_health_retries_rebalance_then_accepts_stable_member() -> None:
    results = iter([_completed(0, "row PreparingRebalance 1\n"), _completed(0, "row Stable 1\n")])
    assert kafka_health.wait_for_healthy_group(
        ["fake"],
        timeout_seconds=10,
        interval_seconds=0,
        runner=lambda _: next(results),
        monotonic=lambda: 0,
        sleeper=lambda _: None,
    )


@pytest.mark.parametrize(
    "result",
    [
        _completed(0, "row Stable 0\n"),
        _completed(0, "Consumer group does not exist.\n"),
        _completed(2, stderr="broker unavailable"),
    ],
)
def test_kafka_health_rejects_zero_members_absent_group_and_command_failure(
    result: subprocess.CompletedProcess[str],
) -> None:
    assert not kafka_health.wait_for_healthy_group(
        ["fake"], timeout_seconds=0, interval_seconds=0, runner=lambda _: result
    )


class ComposeRunner:
    def __init__(
        self, *, running: bool = True, counts: Sequence[int] = (), query_error: bool = False
    ) -> None:
        self.running = running
        self.counts = iter(counts)
        self.query_error = query_error
        self.query_calls = 0

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if "ps" in command:
            return _completed(0, "crawler-id\n" if self.running else "")
        self.query_calls += 1
        if self.query_error:
            return _completed(2, stderr="database unavailable")
        return _completed(0, f"{next(self.counts)}\n")


def test_crawler_drain_treats_missing_crawler_as_first_deploy() -> None:
    runner = ComposeRunner(running=False, counts=[0])
    assert (
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)
        == crawler_drain.NO_CRAWLER
    )
    assert runner.query_calls == 1


def test_missing_crawler_with_persisted_work_does_not_bypass_drain() -> None:
    runner = ComposeRunner(running=False, counts=[2])
    assert (
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)
        == 3
    )
    assert runner.query_calls == 1


def test_missing_crawler_still_fails_closed_on_db_query_error() -> None:
    runner = ComposeRunner(running=False, query_error=True)
    with pytest.raises(crawler_drain.CrawlStateError, match="database query failed"):
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)


def test_inconsistent_crawler_schema_is_not_an_empty_queue() -> None:
    runner = ComposeRunner(counts=[-1])
    with pytest.raises(crawler_drain.CrawlStateError, match="schema is incomplete"):
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)


def test_crawler_database_query_handles_new_schema_and_incomplete_schema() -> None:
    assert "to_regclass('public.crawl_runs')" in crawler_drain.SCHEMA_QUERY
    assert "pg_catalog.pg_tables" in crawler_drain.SCHEMA_QUERY
    assert 'case "$state"' in crawler_drain.QUERY_COMMAND


def test_crawler_state_subprocess_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("docker", 35)

    monkeypatch.setattr(crawler_drain.subprocess, "run", hang)
    with pytest.raises(crawler_drain.CrawlStateError, match="timed out"):
        crawler_drain.wait_for_drain(["docker", "compose"], timeout_seconds=600, poll_seconds=10)


def test_crawler_drain_continues_immediately_without_active_run() -> None:
    runner = ComposeRunner(counts=[0])
    assert (
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)
        == 0
    )


def test_crawler_drain_waits_until_active_run_finishes() -> None:
    runner = ComposeRunner(counts=[1, 0])
    assert (
        crawler_drain.wait_for_drain(
            ["compose"],
            timeout_seconds=10,
            poll_seconds=0,
            runner=runner,
            monotonic=lambda: 0,
            sleeper=lambda _: None,
        )
        == 0
    )
    assert runner.query_calls == 2


def test_crawler_drain_returns_distinct_active_timeout() -> None:
    runner = ComposeRunner(counts=[1])
    assert (
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=0, poll_seconds=0, runner=runner)
        == 3
    )


def test_crawler_drain_query_failure_is_not_an_interruption_choice() -> None:
    runner = ComposeRunner(query_error=True)
    with pytest.raises(crawler_drain.CrawlStateError, match="database query failed"):
        crawler_drain.wait_for_drain(["compose"], timeout_seconds=10, poll_seconds=0, runner=runner)
    assert runner.query_calls == 1


def test_crawler_final_race_check_rejects_new_active_run() -> None:
    assert (
        crawler_drain.final_check(
            ["compose"], allow_active_interruption=False, runner=ComposeRunner(counts=[1])
        )
        == 3
    )


def test_crawler_final_check_queries_persisted_state_even_if_crawler_stopped() -> None:
    runner = ComposeRunner(running=False, counts=[1])
    assert (
        crawler_drain.final_check(["compose"], allow_active_interruption=False, runner=runner) == 3
    )
    assert runner.query_calls == 1


def test_crawler_final_race_check_honors_explicit_authorization() -> None:
    assert (
        crawler_drain.final_check(
            ["compose"], allow_active_interruption=True, runner=ComposeRunner(counts=[1])
        )
        == 0
    )


def test_crawler_wait_cli_parses_flags_after_mode_before_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float, float]] = []

    def fake_wait(compose: Sequence[str], *, timeout_seconds: float, poll_seconds: float) -> int:
        calls.append((list(compose), timeout_seconds, poll_seconds))
        return crawler_drain.NO_CRAWLER

    monkeypatch.setattr(crawler_drain, "wait_for_drain", fake_wait)
    assert (
        crawler_drain.main(
            [
                "crawler_drain.py",
                "wait",
                "--timeout",
                "600",
                "--poll",
                "10",
                "--",
                "docker",
                "compose",
                "--project-name",
                "sahabino",
            ]
        )
        == crawler_drain.NO_CRAWLER
    )
    assert calls == [(["docker", "compose", "--project-name", "sahabino"], 600.0, 10.0)]


def test_crawler_final_cli_passes_interruption_flag_not_as_compose_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_final(compose: Sequence[str], *, allow_active_interruption: bool) -> int:
        calls.append((list(compose), allow_active_interruption))
        return 0

    monkeypatch.setattr(crawler_drain, "final_check", fake_final)
    assert (
        crawler_drain.main(
            [
                "crawler_drain.py",
                "final",
                "--allow-active-interruption",
                "--",
                "docker",
                "compose",
            ]
        )
        == 0
    )
    assert calls == [(["docker", "compose"], True)]


def test_crawler_drain_refuses_missing_compose_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never_run(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("no Docker command may be launched")

    monkeypatch.setattr(crawler_drain, "wait_for_drain", never_run)
    assert crawler_drain.main(["crawler_drain.py", "wait", "docker", "compose"]) == 2


def test_crawler_drain_missing_executable_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker executable not found")

    monkeypatch.setattr(crawler_drain.subprocess, "run", missing)
    assert (
        crawler_drain.main(
            [
                "crawler_drain.py",
                "wait",
                "--",
                "docker",
                "compose",
            ]
        )
        == 2
    )
    assert "Unable to execute crawler-state command" in capsys.readouterr().err


def test_crawler_drain_failure_assertion_reports_actual_command_error() -> None:
    text = RELEASE_TASKS.read_text()
    assert "sahabino_crawler_drain.stderr" in text
    assert "Crawler-state verification failed" in text
    assert "sahabino_crawler_drain.rc in [0, 3, 4]" in text


def _seaweed_payload(access_key: str = "access", secret_key: str = "secret") -> dict[str, object]:
    return {
        "identities": [
            {
                "name": "production",
                "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                "actions": ["Admin", "Read", "List", "Tagging", "Write"],
            }
        ]
    }


def test_atomic_secret_install_replaces_valid_candidate(tmp_path: pathlib.Path) -> None:
    secret_dir = tmp_path / "seaweedfs"
    secret_dir.mkdir()
    candidate = secret_dir / ".candidate"
    destination = secret_dir / "seaweedfs-s3.json"
    candidate.write_text(json.dumps(_seaweed_payload()), encoding="utf-8")
    current = candidate.stat()
    atomic_config.install_config(
        candidate, destination, tmp_path, uid=current.st_uid, gid=current.st_gid
    )
    assert not candidate.exists()
    assert json.loads(destination.read_text(encoding="utf-8"))["identities"]


def test_atomic_secret_invalid_json_preserves_previous_and_cleans_candidate(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_dir = tmp_path / "seaweedfs"
    secret_dir.mkdir()
    destination = secret_dir / "seaweedfs-s3.json"
    destination.write_text("previous-valid", encoding="utf-8")
    candidate = secret_dir / ".candidate"
    candidate.write_text('{"secretKey":"must-not-be-printed"', encoding="utf-8")
    current = candidate.stat()
    result = atomic_config.main(
        [
            "atomic_seaweedfs_config.py",
            "--candidate",
            str(candidate),
            "--destination",
            str(destination),
            "--allowed-root",
            str(tmp_path),
            "--uid",
            str(current.st_uid),
            "--gid",
            str(current.st_gid),
        ]
    )
    assert result == 1
    assert destination.read_text(encoding="utf-8") == "previous-valid"
    assert not candidate.exists()
    assert "must-not-be-printed" not in capsys.readouterr().err


def test_atomic_secret_rejects_path_escape(tmp_path: pathlib.Path) -> None:
    inside = tmp_path / "runtime"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    candidate = outside / ".candidate"
    candidate.write_text(json.dumps(_seaweed_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        atomic_config.install_config(
            candidate,
            outside / "final.json",
            inside,
            uid=candidate.stat().st_uid,
            gid=candidate.stat().st_gid,
        )


def test_atomic_secret_rejects_symlink_destination(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target"
    target.write_text("old", encoding="utf-8")
    destination = tmp_path / "seaweedfs-s3.json"
    try:
        destination.symlink_to(target)
    except OSError as error:
        pytest.skip(f"local filesystem cannot create symlinks: {error}")
    candidate = tmp_path / ".candidate"
    candidate.write_text(json.dumps(_seaweed_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        atomic_config.install_config(
            candidate,
            destination,
            tmp_path,
            uid=candidate.stat().st_uid,
            gid=candidate.stat().st_gid,
        )


def test_production_env_validator_requires_old_and_network_keys(tmp_path: pathlib.Path) -> None:
    candidate = tmp_path / ".env"
    candidate.write_text(
        "\n".join(f"{key}=value" for key in sorted(environment_validator.REQUIRED_KEYS)) + "\n",
        encoding="utf-8",
    )
    environment_validator.validate_environment(candidate)
    candidate.write_text("POSTGRES_DB=value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required variables"):
        environment_validator.validate_environment(candidate)

    complete = {key: "value" for key in environment_validator.REQUIRED_KEYS}
    complete["SAHABINO_OBJECT_STORAGE_SECRET_KEY"] = ""
    candidate.write_text(
        "\n".join(f"{key}={value}" for key, value in sorted(complete.items())) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="required variable .* is empty"):
        environment_validator.validate_environment(candidate)


def _bash_path() -> str | None:
    if os.name == "nt":
        git_bash = pathlib.Path("C:/Program Files/Git/bin/bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _bash_path()


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("a working Bash interpreter is unavailable")
    merged = os.environ.copy()
    merged["NO_COLOR"] = "1"
    if env:
        merged.update(env)
    if os.name == "nt":
        merged["SAHABINO_PYTHON3_BIN"] = (ROOT / ".venv" / "Scripts" / "python.exe").as_posix()
    return subprocess.run(
        [BASH, "-c", script],
        cwd=ROOT,
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )


def test_compose_builder_applies_both_profiles_to_every_operation(tmp_path: pathlib.Path) -> None:
    app = tmp_path / "app"
    bin_dir = tmp_path / "bin"
    app.mkdir()
    bin_dir.mkdir()
    (app / ".env").write_text("SAFE=value\n", encoding="utf-8")
    capture = tmp_path / "calls"
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n', encoding="utf-8", newline="\n"
    )
    docker.chmod(0o755)
    result = _run_bash(
        f"""
source '{ASSISTANT.as_posix()}'
APP_DIR='{app.as_posix()}'
COMPOSE_PROFILES=(observability network)
compose config --services
compose ps
compose logs api
compose exec -T api true
compose run --rm api true
compose up api
compose build api
""",
        env={"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr
    calls = capture.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 7
    for call in calls:
        assert "--profile observability --profile network" in call
        assert '--profile "observability network"' not in call


@pytest.mark.parametrize("mode", ["check", "provision", "deploy", "verify", "full"])
@pytest.mark.parametrize("assume_yes", [0, 1])
def test_assistant_mode_dispatch_does_not_cross_mode_boundaries(
    tmp_path: pathlib.Path, mode: str, assume_yes: int
) -> None:
    calls = tmp_path / "mode-calls"
    functions = (
        "setup_logging require_root_for_bootstrap ubuntu_preflight ensure_base_packages "
        "ensure_deploy_user discover_repository_url ensure_docker ensure_deploy_key_and_alias "
        "ensure_repository load_project_config check_reserved_ports ensure_ansible_controller "
        "acquire_deployment_lock resolve_revision bootstrap_selected_release "
        "precheckout_backup_and_checkout ensure_local_inventory ensure_vault run_ansible_syntax_check run_provision run_deploy "
        "verify_deployment finalize_deployment_revision"
    )
    result = _run_bash(
        f"""
source '{ASSISTANT.as_posix()}'
CALLS='{calls.as_posix()}'
record() {{ printf '%s\\n' "$1" >> "$CALLS"; }}
for name in {functions}; do eval "$name() {{ record $name; }}"; done
MODE={mode}
ASSUME_YES={assume_yes}
APP_DIR='{(tmp_path / "missing-app").as_posix()}'
main
"""
    )
    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8").splitlines()
    if mode == "verify":
        assert "verify_deployment" in invoked
        assert "run_provision" not in invoked
        assert "run_deploy" not in invoked
    elif mode == "check":
        assert "run_ansible_syntax_check" in invoked
        assert "run_provision" not in invoked
        assert "run_deploy" not in invoked
        assert "verify_deployment" not in invoked
    elif mode == "provision":
        assert "run_provision" in invoked
        assert "run_deploy" not in invoked
        assert "verify_deployment" not in invoked
    else:
        assert invoked[-4:] == [
            "run_provision",
            "run_deploy",
            "verify_deployment",
            "finalize_deployment_revision",
        ]


@pytest.mark.parametrize(
    ("setup", "expected_status", "expected"),
    [
        (
            "ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=; OBJECT_STORAGE_PUBLIC_ENDPOINT=",
            0,
            "private|127.0.0.1",
        ),
        ("ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=public; OBJECT_STORAGE_PUBLIC_ENDPOINT=", 1, ""),
        (
            "ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=public; "
            "OBJECT_STORAGE_PUBLIC_ENDPOINT=https://storage.example.org; CONFIRM_PUBLIC_OBJECT_STORAGE=0",
            1,
            "",
        ),
        (
            "ASSUME_YES=1; OBJECT_STORAGE_EXPOSURE=public; "
            "OBJECT_STORAGE_PUBLIC_ENDPOINT=https://storage.example.org; CONFIRM_PUBLIC_OBJECT_STORAGE=1",
            0,
            "public|0.0.0.0",
        ),
    ],
)
def test_noninteractive_storage_exposure_contract(
    setup: str, expected_status: int, expected: str
) -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; {setup}; "
        "configure_object_storage_exposure; rc=$?; "
        'printf "RESULT:%s|%s|%s\\n" "$rc" "$OBJECT_STORAGE_EXPOSURE" "$OBJECT_STORAGE_BIND_ADDRESS"; exit "$rc"'
    )
    assert result.returncode == expected_status
    if expected:
        assert f"RESULT:0|{expected}" in result.stdout


def test_interactive_storage_default_is_private() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; ASSUME_YES=0; OBJECT_STORAGE_EXPOSURE=; "
        "OBJECT_STORAGE_PUBLIC_ENDPOINT=; configure_object_storage_exposure <<< ''; "
        'printf "RESULT:%s|%s\\n" "$OBJECT_STORAGE_EXPOSURE" "$OBJECT_STORAGE_BIND_ADDRESS"'
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT:private|127.0.0.1" in result.stdout


def test_interactive_public_prompts_for_endpoint_and_acknowledgement() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; ASSUME_YES=0; OBJECT_STORAGE_EXPOSURE=; "
        "OBJECT_STORAGE_PUBLIC_ENDPOINT=; CONFIRM_PUBLIC_OBJECT_STORAGE=0; "
        "configure_object_storage_exposure <<< $'2\\nhttps://storage.example.org\\ny\\n'; "
        'printf "RESULT:%s|%s|%s\\n" "$OBJECT_STORAGE_EXPOSURE" "$OBJECT_STORAGE_BIND_ADDRESS" "$CONFIRM_PUBLIC_OBJECT_STORAGE"'
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT:public|0.0.0.0|1" in result.stdout


def test_public_access_hints_keep_observability_and_api_information() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; OBJECT_STORAGE_EXPOSURE=public; "
        "OBJECT_STORAGE_PUBLIC_ENDPOINT=https://storage.example.org; print_access_hints"
    )
    assert result.returncode == 0, result.stderr
    for marker in ("Grafana:", "Loki:", "Alloy:", "API exposure", "no port 8333 SSH forward"):
        assert marker in result.stdout


def test_private_access_hints_include_a_valid_storage_forward() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; OBJECT_STORAGE_EXPOSURE=private; "
        "OBJECT_STORAGE_PUBLIC_ENDPOINT=http://127.0.0.1:8333; print_access_hints"
    )
    assert result.returncode == 0, result.stderr
    assert "-L 8333:127.0.0.1:8333 \\\n    sahabino@SERVER" in result.stdout
    for marker in ("Grafana:", "Loki:", "Alloy:", "API exposure"):
        assert marker in result.stdout


def test_early_seaweed_rollout_does_not_recreate_data_before_backup() -> None:
    release = RELEASE_TASKS.read_text(encoding="utf-8")
    start = release.index(
        "- name: Ensure SeaweedFS is running without recreating an existing data-bearing container"
    )
    storage_init = release.index(
        "- name: Initialize or verify the production capture bucket idempotently"
    )
    crawler_drain = release.index("- name: Wait for persisted crawler work to drain")
    early_rollout = release[start:storage_init]

    assert start < storage_init < crawler_drain
    assert "release_containers.py" in early_rollout
    assert "'ensure'" in early_rollout
    assert "--force-recreate" not in early_rollout
    assert "down" not in early_rollout


def test_seaweed_verification_uses_dynamic_exact_identity_and_docker_port_data() -> None:
    verify = VERIFY_TASKS.read_text(encoding="utf-8")
    assistant = ASSISTANT.read_text(encoding="utf-8")
    identity_start = verify.index(
        "- name: Verify the staged SeaweedFS credential and exact PID 1 identity"
    )
    identity_end = verify.index("- name: Verify the configured capture bucket without creating it")
    identity = verify[identity_start:identity_end]
    assistant_identity_start = assistant.index("verify_network_runtime()")
    assistant_identity_end = assistant.index(
        "compose exec -T api uv run --no-sync python -c", assistant_identity_start
    )
    assistant_identity = assistant[assistant_identity_start:assistant_identity_end]

    port_verification_start = verify.index("- name: Resolve the running SeaweedFS container ID")
    port_verification_end = verify.index(
        "- name: Inspect the root-protected SeaweedFS host directory and credential"
    )
    port_verification = verify[port_verification_start:port_verification_end]

    assert "community.docker.docker_container_info" not in verify
    assert "ansible.builtin.command" in port_verification
    assert (
        "\n      - docker\n      - inspect\n      - --type\n      - container\n"
        in port_verification
    )
    assert "from_json" in port_verification
    assert "NetworkSettings.Ports['8333/tcp']" in port_verification
    assert "changed_when: false" in port_verification
    assert "no_log: true" in port_verification
    assert "sahabino_expected_seaweedfs_host_ip" in verify
    assert "verify_seaweedfs_port_binding || return 1" in assistant_identity
    # The old duplicated Jinja/shell fragment escaped awk quotes incorrectly:
    # under `sh -eu`, $2 was expanded as a missing shell argument. Assert that
    # BOTH callers execute the same read-only runtime verifier instead.
    shared_audit = SEAWEED_WRAPPER.read_text(encoding="utf-8")
    for marker in (
        "id -u seaweed",
        "id -g seaweed",
        "/^Uid:/",
        "/^Gid:/",
        '"$pid1_uid" = "$seaweed_uid"',
        '"$pid1_gid" = "$seaweed_gid"',
    ):
        assert marker in shared_audit
    assert "--verify-runtime" in identity
    assert "--verify-runtime" in assistant_identity
    assert "'sh', '-s', '--', '--verify-runtime'" in identity
    assert "stdin_add_newline: false" in identity
    assert "seaweedfs-entrypoint.sh" in identity
    assert "sh -s -- --verify-runtime" in assistant_identity
    assert "seaweedfs-entrypoint.sh" in assistant_identity
    # The container may still carry an older inode for its single-file bind.
    assert "/usr/local/bin/sahabino-seaweedfs-entrypoint" not in identity + assistant_identity
    assert "pid1_uid=$(awk" not in identity + assistant_identity
    assert "1000" not in identity + assistant_identity + shared_audit
    assert "--verify-runtime" in RELEASE_TASKS.read_text()
    syntax = subprocess.run(["sh", "-n", str(SEAWEED_WRAPPER)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize(
    ("exposure", "configured_host", "actual_host", "expected_returncode"),
    [
        ("private", "127.0.0.1", "127.0.0.1", 0),
        ("public", "0.0.0.0", "0.0.0.0", 0),
        ("private", "127.0.0.1", "0.0.0.0", 1),
        ("public", "0.0.0.0", "127.0.0.1", 1),
    ],
)
def test_assistant_verifies_actual_seaweed_port_binding(
    tmp_path: pathlib.Path,
    exposure: str,
    configured_host: str,
    actual_host: str,
    expected_returncode: int,
) -> None:
    bindings = json.dumps({"8333/tcp": [{"HostIp": actual_host, "HostPort": "8333"}]})
    python3 = tmp_path / "python3"
    python3.write_text(
        f"#!/bin/sh\nexec '{pathlib.Path(sys.executable).as_posix()}' \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    python3.chmod(0o755)
    result = _run_bash(
        f"""
source '{ASSISTANT.as_posix()}'
compose() {{ printf 'seaweed-container-id\\n'; }}
docker() {{ printf '%s\\n' '{bindings}'; }}
OBJECT_STORAGE_EXPOSURE='{exposure}'
OBJECT_STORAGE_BIND_ADDRESS='{configured_host}'
verify_seaweedfs_port_binding
""",
        env={"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == expected_returncode
    if expected_returncode:
        assert "SeaweedFS exposure mismatch" in result.stderr


def test_ci_runs_deployment_regressions_and_all_tracked_shell_syntax() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "python -m pytest tests/deployment" in workflow
    assert "git ls-files -z '*.sh' | xargs -0 -n1 bash -n" in workflow


def test_nobody_audit_uses_numeric_uid_and_primary_gid() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; "
        "id() { if [[ $1 == -u ]]; then printf '65534\\n'; else printf '65533\\n'; fi; }; "
        'run_as_root() { local arg; for arg in "$@"; do [[ $arg == -r ]] && return 1; done; return 0; }; '
        "audit_unrelated_user_cannot_read /synthetic/secret"
    )
    assert result.returncode == 0, result.stderr


def test_nobody_audit_reports_actual_readability_not_identity_setup_failure() -> None:
    result = _run_bash(
        f"source '{ASSISTANT.as_posix()}'; "
        "id() { if [[ $1 == -u ]]; then printf '65534\\n'; else printf '65533\\n'; fi; }; "
        "run_as_root() { return 0; }; audit_unrelated_user_cannot_read /synthetic/secret"
    )
    assert result.returncode == 1
    assert "readable by the unrelated nobody account" in result.stderr


def _fake_wrapper_tools(bin_dir: pathlib.Path, *, readable: bool) -> None:
    scripts = {
        "id": """#!/bin/sh
if [ "$*" = "-u" ]; then echo 0
elif [ "$*" = "-u seaweed" ]; then echo 1000
elif [ "$*" = "-g seaweed" ]; then echo 1000
else exit 1
fi
""",
        "chown": "#!/bin/sh\nexit 0\n",
        "su-exec": (
            "#!/bin/sh\nshift\n"
            + (
                "case \" $* \" in *' test -r '*) exit 0 ;; *' test -w '*) exit 1 ;; esac\n"
                if readable
                else "exit 1\n"
            )
            + '"$@"\n'
        ),
    }
    for name, content in scripts.items():
        path = bin_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="MSYS cannot emulate Linux root/account semantics")
def test_seaweed_wrapper_stages_secret_then_delegates_to_upstream(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    runtime = tmp_path / "runtime"
    bin_dir.mkdir()
    _fake_wrapper_tools(bin_dir, readable=True)
    source = tmp_path / "host-secret.json"
    source.write_text(json.dumps(_seaweed_payload()), encoding="utf-8")
    called = tmp_path / "upstream-called"
    upstream = tmp_path / "entrypoint.sh"
    upstream.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" > "{called.as_posix()}"\n', encoding="utf-8", newline="\n"
    )
    upstream.chmod(0o755)
    result = _run_bash(
        f"PATH='{bin_dir.as_posix()}':$PATH '{SEAWEED_WRAPPER.as_posix()}' server -s3",
        env={
            "SAHABINO_SEAWEEDFS_CONFIG_INPUT": source.as_posix(),
            "SAHABINO_SEAWEEDFS_RUNTIME_DIR": runtime.as_posix(),
            "SAHABINO_SEAWEEDFS_UPSTREAM_ENTRYPOINT": upstream.as_posix(),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (runtime / "seaweedfs-s3.json").is_file()
    assert "server -s3 -s3.config=" in called.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="MSYS cannot emulate Linux root/account semantics")
def test_seaweed_wrapper_failure_preserves_previous_and_cleans_candidate(
    tmp_path: pathlib.Path,
) -> None:
    bin_dir = tmp_path / "bin"
    runtime = tmp_path / "runtime"
    bin_dir.mkdir()
    runtime.mkdir()
    _fake_wrapper_tools(bin_dir, readable=False)
    previous = runtime / "seaweedfs-s3.json"
    previous.write_text("previous-valid", encoding="utf-8")
    source = tmp_path / "host-secret.json"
    source.write_text('{"secretKey":"never-print-this"}', encoding="utf-8")
    upstream = tmp_path / "entrypoint.sh"
    upstream.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    upstream.chmod(0o755)
    result = _run_bash(
        f"PATH='{bin_dir.as_posix()}':$PATH '{SEAWEED_WRAPPER.as_posix()}' server -s3",
        env={
            "SAHABINO_SEAWEEDFS_CONFIG_INPUT": source.as_posix(),
            "SAHABINO_SEAWEEDFS_RUNTIME_DIR": runtime.as_posix(),
            "SAHABINO_SEAWEEDFS_UPSTREAM_ENTRYPOINT": upstream.as_posix(),
        },
    )
    assert result.returncode == 1
    assert previous.read_text(encoding="utf-8") == "previous-valid"
    assert not list(runtime.glob(".seaweedfs-s3.*"))
    assert "never-print-this" not in result.stdout + result.stderr
