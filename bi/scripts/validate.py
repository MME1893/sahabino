#!/usr/bin/env python3
"""Offline/static safety checks for the standalone Sahabino BI module."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BI_ROOT.parent
QUESTIONS = BI_ROOT / "questions"

EXPECTED_QUESTIONS = {
    "q01_portfolio_current.sql",
    "q02_store_rating_daily.sql",
    "q03_store_rating_vs_category.sql",
    "q04_store_counts_growth.sql",
    "q05_install_milestones.sql",
}

FORBIDDEN_SQL = re.compile(
    r"\b(?:ALTER|CALL|COPY|CREATE|DELETE|DO|DROP|GRANT|INSERT|MERGE|REVOKE|"
    r"TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def require_all(text: str, fragments: tuple[str, ...], context: str) -> None:
    lowered = text.lower()
    missing = [fragment for fragment in fragments if fragment.lower() not in lowered]
    require(not missing, f"{context}: missing required fragments: {', '.join(missing)}")


def strip_sql_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\r\n]*", " ", without_blocks)


def check_required_files() -> None:
    required = {
        BI_ROOT / "compose.yml",
        BI_ROOT / ".env.example",
        BI_ROOT / ".gitignore",
        BI_ROOT / "README.md",
        BI_ROOT / "reports" / "README.md",
        BI_ROOT / "provisioning" / "roles_and_grants.sql.example",
    }
    missing = sorted(str(path.relative_to(BI_ROOT)) for path in required if not path.is_file())
    require(not missing, f"missing module files: {', '.join(missing)}")

    actual_questions = {path.name for path in QUESTIONS.glob("*.sql")}
    expected_names = sorted(EXPECTED_QUESTIONS)
    actual_names = sorted(actual_questions)
    require(
        actual_questions == EXPECTED_QUESTIONS,
        f"question set differs: expected {expected_names}, got {actual_names}",
    )


def check_compose() -> None:
    compose = (BI_ROOT / "compose.yml").read_text(encoding="utf-8")
    require(re.search(r"(?m)^name:\s+sahabino-bi\s*$", compose) is not None, "project name")
    require(re.search(r"(?m)^\s{2}metabase:\s*$", compose) is not None, "metabase service")
    require("restart: unless-stopped" in compose, "restart policy")
    require(
        '"127.0.0.1:${METABASE_PORT:-3001}:3000"' in compose,
        "loopback port 3001 mapping",
    )
    require("depends_on:" not in compose, "standalone service must not use depends_on")
    require("external: true" in compose, "database network must be external")
    require("MB_DB_PASS_FILE: /run/secrets/metabase_app_db_password" in compose, "password file")
    require("MB_DB_TYPE: postgres" in compose, "PostgreSQL application database")
    require("MB_ENCRYPTION_SECRET_KEY" in compose, "encryption key")
    require("MB_SESSION_SECRET_KEY" in compose, "session key")
    require("metabase_app_db_password:" in compose, "Compose secret declaration")
    require("0.0.0.0:${METABASE_PORT" not in compose, "Metabase must not bind all interfaces")

    image_match = re.search(
        r"image:\s*metabase/metabase:(v\d+\.\d+\.\d+)@sha256:([0-9a-f]{64})", compose
    )
    require(image_match is not None, "Metabase image must use exact patch tag and sha256 digest")
    require(image_match.group(1) == "v0.63.18", "unexpected Metabase patch tag")
    require(
        image_match.group(2) == "1160b570cb11c107bce00e71293552df8a8363e01a32c2c7a048cee002dc8a73",
        "unexpected Metabase image digest",
    )

    forbidden_dependencies = ("api", "kafka", "worker", "seaweed", "grafana")
    service_block = compose.split("services:", 1)[1].split("secrets:", 1)[0].lower()
    for dependency in forbidden_dependencies:
        require(
            re.search(rf"(?m)^\s{{2}}{re.escape(dependency)}[^:]*:\s*$", service_block) is None,
            f"forbidden service in BI Compose: {dependency}",
        )


def check_sql_safety_and_shape() -> None:
    sql_by_name: dict[str, str] = {}
    for path in sorted(QUESTIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        executable = strip_sql_comments(sql).strip()
        require(
            executable.upper().startswith(("SELECT", "WITH")), f"{path.name}: must be SELECT/WITH"
        )
        require(FORBIDDEN_SQL.search(executable) is None, f"{path.name}: write/DDL keyword found")
        require(executable.endswith(";"), f"{path.name}: final semicolon missing")
        require(";" not in executable[:-1], f"{path.name}: multiple SQL statements")
        require(
            "review_observations" not in executable.lower(), f"{path.name}: review SQL is deferred"
        )
        require("sentiment_" not in executable.lower(), f"{path.name}: sentiment SQL is deferred")
        sql_by_name[path.name] = executable

    q01 = sql_by_name["q01_portfolio_current.sql"]
    require_all(
        q01,
        (
            "a.package_name <> 'ir.rightel.myrightel'",
            "left join lateral",
            "ct.id = s.crawl_task_id",
            "ct.application_id = s.application_id",
            "ct.status = 'succeeded'",
            "order by s.collected_at desc, s.id desc",
            "snapshot_coverage_status",
        ),
        "Q01",
    )

    q02 = sql_by_name["q02_store_rating_daily.sql"]
    require_all(
        q02,
        (
            "row_number() over",
            "ct.id = s.crawl_task_id",
            "ct.application_id = s.application_id",
            "ct.country_code as crawl_country_code",
            "ct.language_code as crawl_language_code",
            "at time zone 'utc'",
            "order by collected_at desc, snapshot_id desc",
            "snapshots_in_day",
        ),
        "Q02",
    )
    require(
        re.search(
            r"partition\s+by\s+application_id\s*,\s*crawl_country_code\s*,\s*"
            r"crawl_language_code\s*,\s*snapshot_day_utc",
            q02,
            flags=re.IGNORECASE,
        )
        is not None,
        "Q02: daily row number/count must partition by app + actual locale + UTC day",
    )

    for question_name in (
        "q02_store_rating_daily.sql",
        "q03_store_rating_vs_category.sql",
        "q04_store_counts_growth.sql",
        "q05_install_milestones.sql",
    ):
        require_all(
            sql_by_name[question_name],
            (
                "ct.id = s.crawl_task_id",
                "ct.application_id = s.application_id",
                "ct.task_type = 'app_details'",
                "ct.status = 'succeeded'",
                "ct.country_code as crawl_country_code",
                "ct.language_code as crawl_language_code",
            ),
            question_name,
        )

    q03 = sql_by_name["q03_store_rating_vs_category.sql"]
    require_all(
        q03,
        (
            "ac.is_primary is true",
            "count(distinct peer.application_id)",
            "peer.application_id <> focal.application_id",
            "peer.crawl_country_code = focal.crawl_country_code",
            "peer.crawl_language_code = focal.crawl_language_code",
            "peer.snapshot_day_utc = focal.snapshot_day_utc",
            "when peers.peer_app_count >= 2",
            "insufficient_one_peer",
            "limited_two_peer_sample",
        ),
        "Q03",
    )

    q04 = sql_by_name["q04_store_counts_growth.sql"]
    require_all(
        q04,
        (
            "lag(ratings_count) over observation_window",
            "lag(reviews_count) over observation_window",
            "partition by application_id, crawl_country_code, crawl_language_code",
            "ratings_count - previous_ratings_count",
            "reviews_count - previous_reviews_count",
            "ratings_count_negative_correction",
            "reviews_count_negative_correction",
            "days_since_previous_observation",
        ),
        "Q04",
    )
    require("greatest(" not in q04.lower(), "Q04: negative deltas must not be clamped")

    q05 = sql_by_name["q05_install_milestones.sql"]
    require_all(
        q05,
        (
            "lag(min_installs) over observation_window",
            "partition by application_id, crawl_country_code, crawl_language_code",
            "min_installs_threshold",
            "newly_observed_higher_threshold",
            "lower_revision",
            "is_negative_threshold_revision",
            "days_since_previous_observation",
        ),
        "Q05",
    )
    require("generate_series" not in q05.lower(), "Q05: must not fabricate calendar dates")


def check_provisioning_and_documentation() -> None:
    provisioning = (BI_ROOT / "provisioning" / "roles_and_grants.sql.example").read_text(
        encoding="utf-8"
    )
    require_all(
        provisioning,
        (
            "CREATE DATABASE metabase_app",
            "CREATE ROLE sahabino_bi_reader",
            "SET default_transaction_read_only = on",
            "SET statement_timeout = '30s'",
            "GRANT USAGE ON SCHEMA public TO sahabino_bi_reader",
            "GRANT SELECT (",
            "REVOKE TEMPORARY ON DATABASE sahabino FROM sahabino_bi_reader",
        ),
        "provisioning",
    )
    require("GRANT pg_read_all_data" not in provisioning, "reader must not get pg_read_all_data")
    require("GRANT SELECT ON ALL" not in provisioning, "reader must not get schema-wide SELECT")
    require("public.reviews" not in provisioning, "raw reviews table must not be granted")
    require("review_observations" not in provisioning, "review observations must not be granted")
    require(
        "PASSWORD '" not in provisioning.upper(), "tracked provisioning must not hold passwords"
    )

    env_example = (BI_ROOT / ".env.example").read_text(encoding="utf-8")
    require("CHANGE_ME" in env_example, ".env.example must use key placeholders")
    require(
        "sahabino_bi_reader" not in (BI_ROOT / "compose.yml").read_text(encoding="utf-8"),
        "source reader credential must not be a Compose environment setting",
    )

    readme = (BI_ROOT / "README.md").read_text(encoding="utf-8")
    reports = (BI_ROOT / "reports" / "README.md").read_text(encoding="utf-8")
    require_all(
        readme,
        (
            "ssh -N -L 3001:127.0.0.1:3001",
            "Reconnect after PostgreSQL recreation",
            "Metadata backup and restore boundary",
            "model persistence",
            "No production, existing development database, or live Metabase was contacted",
        ),
        "operator README",
    )
    require_all(
        reports,
        (
            "ir.rightel.myrightel",
            "Review Intelligence",
            "Network/Application Comparison",
            "Release Impact",
            "insufficient_one_peer",
        ),
        "report catalog",
    )


def git_status_paths() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"').replace("\\", "/"))
    return paths


def check_repository_scope() -> str:
    if not (REPO_ROOT / ".git").exists():
        return "git scope check skipped (standalone copy)"

    try:
        changed_paths = git_status_paths()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValidationError(f"could not inspect git scope: {error}") from error

    outside_bi = [path for path in changed_paths if not path.startswith("bi/")]
    require(not outside_bi, f"changes outside bi/: {', '.join(outside_bi)}")

    protected = {
        "docker-compose.yml",
        "compose.prod.yml",
        "migrations",
        "ansible",
        "deploy",
        "scripts",
    }
    for path in changed_paths:
        first = path.split("/", 1)[0]
        require(path not in protected and first not in protected, f"protected path changed: {path}")

    production_compose = (REPO_ROOT / "compose.prod.yml").read_text(encoding="utf-8")
    require_all(
        production_compose,
        ("target: 3000", 'published: "3000"', "host_ip: 127.0.0.1"),
        "existing Grafana loopback mapping",
    )
    return f"git scope contains only bi/ ({len(changed_paths)} changed files)"


def main() -> int:
    checks: list[tuple[str, object]] = [
        ("required files", check_required_files),
        ("Compose isolation and pinning", check_compose),
        ("SQL safety and analytic guards", check_sql_safety_and_shape),
        ("least-privilege provisioning and handover", check_provisioning_and_documentation),
    ]

    try:
        for label, check in checks:
            check()
            print(f"PASS: {label}")
        print(f"PASS: {check_repository_scope()}")
    except ValidationError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("PASS: standalone BI static validation complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
