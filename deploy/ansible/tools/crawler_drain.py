#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

QUERY = "SELECT COUNT(*) FROM crawl_runs WHERE status IN ('pending', 'running');"
QUERY_COMMAND = (
    "exec psql --no-psqlrc --tuples-only --no-align "
    '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --command "$1"'
)
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
NO_CRAWLER = 4


class CrawlStateError(RuntimeError):
    pass


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def crawler_is_running(compose: Sequence[str], runner: Runner) -> bool:
    result = runner([*compose, "ps", "--quiet", "crawler"])
    if result.returncode != 0:
        raise CrawlStateError("Compose could not determine whether crawler is running")
    return bool(result.stdout.strip())


def active_crawl_count(compose: Sequence[str], runner: Runner) -> int:
    result = runner(
        [
            *compose,
            "exec",
            "-T",
            "postgres",
            "sh",
            "-eu",
            "-c",
            QUERY_COMMAND,
            "sh",
            QUERY,
        ]
    )
    if result.returncode != 0:
        raise CrawlStateError("authoritative crawler-state database query failed")
    try:
        count = int(result.stdout.strip())
    except ValueError as error:
        raise CrawlStateError("crawler-state query returned an invalid count") from error
    if count < 0:
        raise CrawlStateError("crawler-state query returned a negative count")
    return count


def wait_for_drain(
    compose: Sequence[str],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    runner: Runner = _default_runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    if not crawler_is_running(compose, runner):
        print("Crawler is not running; treating this as a first deployment.")
        return NO_CRAWLER
    deadline = monotonic() + timeout_seconds
    while True:
        count = active_crawl_count(compose, runner)
        if count == 0:
            print("No active persisted crawl run remains.")
            return 0
        print(f"Waiting for {count} active persisted crawl run(s) to finish.")
        if monotonic() >= deadline:
            return 3
        sleeper(min(poll_seconds, max(0.0, deadline - monotonic())))


def final_check(
    compose: Sequence[str], *, allow_active_interruption: bool, runner: Runner = _default_runner
) -> int:
    count = active_crawl_count(compose, runner)
    if count == 0:
        return 0
    if allow_active_interruption:
        print(f"Explicit authorization permits interruption of {count} active crawl run(s).")
        return 0
    print(f"Final race-window check found {count} active crawl run(s).", file=sys.stderr)
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("wait", "final"))
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--allow-active-interruption", action="store_true")
    parser.add_argument("compose", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str]) -> int:
    arguments = _parser().parse_args(argv[1:])
    compose = arguments.compose
    if compose and compose[0] == "--":
        compose = compose[1:]
    if not compose:
        print("crawler drain requires a Compose command", file=sys.stderr)
        return 2
    try:
        if arguments.mode == "wait":
            return wait_for_drain(
                compose,
                timeout_seconds=arguments.timeout,
                poll_seconds=arguments.poll,
            )
        return final_check(
            compose,
            allow_active_interruption=arguments.allow_active_interruption,
        )
    except CrawlStateError as error:
        print(f"Crawler-state verification failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
