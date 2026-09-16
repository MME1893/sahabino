#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

STATE_PATTERN = re.compile(
    r"\b(?P<state>Stable|PreparingRebalance|CompletingRebalance|Empty|Dead|Unknown)"
    r"\s+(?P<members>\d+)\s*$"
)
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def parse_group_state(output: str) -> tuple[str, int] | None:
    for line in reversed(output.splitlines()):
        match = STATE_PATTERN.search(line.strip())
        if match:
            return match.group("state"), int(match.group("members"))
    return None


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def wait_for_healthy_group(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    runner: Runner = _default_runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = monotonic() + timeout_seconds
    last_detail = "group state was unavailable"
    while True:
        result = runner(command)
        if result.returncode == 0:
            state = parse_group_state(result.stdout)
            if state is not None:
                group_state, members = state
                last_detail = f"state={group_state} members={members}"
                if group_state == "Stable" and members >= 1:
                    print(f"Kafka analyzer consumer is healthy: {last_detail}")
                    return True
            else:
                last_detail = "Kafka output did not contain a parseable state/member row"
        else:
            last_detail = f"Kafka command failed with exit {result.returncode}"

        if monotonic() >= deadline:
            print(f"Kafka analyzer consumer did not become healthy: {last_detail}", file=sys.stderr)
            return False
        sleeper(min(interval_seconds, max(0.0, deadline - monotonic())))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--cwd")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str]) -> int:
    arguments = _parser().parse_args(argv[1:])
    command = arguments.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("Kafka group health check requires a command", file=sys.stderr)
        return 2

    def runner(candidate: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            candidate,
            cwd=arguments.cwd,
            check=False,
            capture_output=True,
            text=True,
        )

    return (
        0
        if wait_for_healthy_group(
            command,
            timeout_seconds=arguments.timeout,
            interval_seconds=arguments.interval,
            runner=runner,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
