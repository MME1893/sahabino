#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

# Before the first Alembic migration, a genuinely empty database has no
# application tables.  A partially migrated database is *not* a first install:
# never treat a missing crawl_runs table in a populated schema as an empty queue.
SCHEMA_QUERY = """
SELECT CASE
  WHEN to_regclass('public.crawl_runs') IS NOT NULL THEN 'READY'
  WHEN EXISTS (
    SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public'
  ) THEN 'INCOMPLETE'
  ELSE 'FRESH'
END;
"""
QUERY = "SELECT COUNT(*) FROM public.crawl_runs WHERE status IN ('pending', 'running');"
QUERY_COMMAND = (
    'state="$(psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align '
    '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --command "$1")"; '
    'case "$state" in '
    'FRESH) printf "0\\n" ;; '
    "READY) exec psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align "
    '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --command "$2" ;; '
    '*) printf "Incomplete or unrecognized crawler schema: %s\\n" "$state" >&2; exit 3 ;; '
    "esac"
)
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
NO_CRAWLER = 4


class CrawlStateError(RuntimeError):
    pass


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired as error:
        raise CrawlStateError("crawler-state command timed out after 35 seconds") from error
    except OSError as error:
        raise CrawlStateError(f"Unable to execute crawler-state command: {error}") from error


def crawler_is_running(compose: Sequence[str], runner: Runner) -> bool:
    result = runner([*compose, "ps", "--quiet", "crawler"])
    if result.returncode != 0:
        raise CrawlStateError(
            f"Compose could not determine whether crawler is running: \
                {result.stderr.strip()[-500:]}"
        )
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
            SCHEMA_QUERY,
            QUERY,
        ]
    )
    if result.returncode != 0:
        raise CrawlStateError(
            f"authoritative crawler-state database query failed: {result.stderr.strip()[-500:]}"
        )
    try:
        count = int(result.stdout.strip())
    except ValueError as error:
        raise CrawlStateError("crawler-state query returned an invalid count") from error
    if count < 0:
        raise CrawlStateError(
            "crawler-state schema is incomplete (crawl_runs missing in nonempty database)"
        )
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
    # The container can be absent even when its database has unfinished jobs.
    # Query PostgreSQL FIRST; an absent crawler does not prove an empty queue.
    running = crawler_is_running(compose, runner)
    if not running:
        count = active_crawl_count(compose, runner)
        if count:
            print(
                f"Crawler container absent but {count} persisted crawl run(s) remain; "
                "explicit interruption authorization is required.",
                file=sys.stderr,
            )
            return 3
        print("Crawler is absent and authoritative database state has no pending work.")
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
    return parser


def main(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        _parser().parse_args(["--help"])
        return 0
    # Ansible passes options AFTER `wait`/`final`. argparse.REMAINDER used to
    # swallow these options into the Compose command, attempting to execute
    # `--timeout` or `--allow-active-interruption` instead of `docker`.
    # Split on the explicit separator before parsing our own arguments.
    try:
        separator = argv.index("--", 1)
    except ValueError:
        print("crawler drain requires '--' before the Compose command", file=sys.stderr)
        return 2
    arguments = _parser().parse_args(argv[1:separator])
    compose = argv[separator + 1 :]
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
