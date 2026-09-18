#!/usr/bin/env python3
"""Offline structural safety check ONLY; never substitutes for PostgreSQL or API integration."""

import re
import subprocess
import sys
from pathlib import Path

from metabase_sync import DML, ROOT, SyncError, load_manifest, render_unfiltered


def git_scope_audit():
    try:
        probe = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("NOT RUN: Git scope audit (Git executable is unavailable)")
        return False
    if probe.returncode:
        print("NOT RUN: Git scope audit (standalone BI directory is not inside a Git worktree)")
        return False
    repo = Path(probe.stdout.strip()).resolve()
    try:
        relative = ROOT.resolve().relative_to(repo)
    except ValueError as exc:
        raise AssertionError("BI directory is outside the detected Git worktree") from exc
    bi_prefix = "" if relative == Path(".") else relative.as_posix().rstrip("/") + "/"
    changed = []
    for args in (
        ["diff", "--name-only", "HEAD"],
        ["diff", "--cached", "--name-only", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )
        changed.extend(result.stdout.splitlines())
    assert not bi_prefix or all(
        path == bi_prefix.rstrip("/") or path.startswith(bi_prefix) for path in changed
    )
    assert not any(
        re.fullmatch(re.escape(bi_prefix) + r"questions/q(?:0[1-9]|1[0-9]|2[0-4])_.*\.sql", path)
        for path in changed
    )
    print("PASS: Git scope audit found only BI changes and no Q01-Q24 SQL changes")
    return True


def check():
    manifest = load_manifest()
    compose = (ROOT / "compose.yml").read_text()
    assert (
        "metabase/metabase:v0.63.18@sha256:1160b570cb11c107bce00e71293552df8a8363e01a32c2c7a048cee002dc8a73"
        in compose
    )
    assert "127.0.0.1:${METABASE_PORT:-3001}:3000" in compose
    assert "MB_DB_PASS_FILE:" in compose and "MB_ENCRYPTION_SECRET_KEY:" in compose
    assert "curl --fail --silent --show-error" in compose and "/api/health" in compose
    grants = (ROOT / "provisioning" / "roles_and_grants.sql.example").read_text().lower()
    assert "grant pg_read_all_data" not in grants and "grant select on all tables" not in grants
    for secret in ("author_name", "external_review_id", "content"):
        assert not re.search(r"grant\s+select\s*\([^)]*\b" + secret + r"\b", grants, re.S)
    assert (
        "grant select (version_num)" in grants
        and "grant select (id,application_id,first_observed_at)" in grants
    )
    for secret in ("object_key", "original_filename", "error_message"):
        assert not re.search(r"grant\s+select\s*\([^)]*\b" + secret + r"\b", grants, re.S)
    network_update = (
        (ROOT / "provisioning" / "network_grants_existing.sql.example").read_text().lower()
    )
    assert "create role" not in network_update and "create database" not in network_update
    assert (
        "grant select on" not in network_update and "grant pg_read_all_data" not in network_update
    )
    assert "object_key" in network_update and "analysis_warnings" in network_update
    assert len(manifest["questions"]) == 42 and len(manifest["dashboards"]) == 7
    assert [q["key"] for q in manifest["questions"][:24]] == [f"q{x:02d}" for x in range(1, 25)]
    assert [d["key"] for d in manifest["dashboards"][:4]] == [
        "executive",
        "store",
        "reviews",
        "sentiment",
    ]
    assert [[card["question"] for card in d["cards"]] for d in manifest["dashboards"][:4]] == [
        ["q06", "q01", "q07", "q09", "q08", "q22"],
        ["q02", "q01", "q03", "q04", "q08", "q05", "q18", "q19", "q20", "q21", "q22"],
        ["q11", "q10", "q12", "q23", "q24"],
        ["q13", "q14", "q15", "q16", "q17"],
    ]
    files = {p.name for p in (ROOT / "questions").glob("*.sql")}
    assert files == {Path(q["sql"]).name for q in manifest["questions"]}
    for q in manifest["questions"]:
        sql = (ROOT / q["sql"]).read_text()
        clean = re.sub(r"/\*.*?\*/|--[^\n]*", " ", render_unfiltered(sql), flags=re.S)
        assert clean.strip().upper().startswith(("SELECT", "WITH")) and not DML.search(clean)
        assert "reviews.content" not in sql and "review_observations.content" not in sql
        assert "network_captures.object_key" not in sql and "original_filename" not in sql
    assert "ir.rightel.myrightel" in (ROOT / "questions" / "q01_portfolio_current.sql").read_text()
    assert "crawl_tasks" in (ROOT / "questions" / "q02_store_rating_daily.sql").read_text()
    assert "pair_id" in (ROOT / "questions" / "q32_network_paired_comparison.sql").read_text()
    for n in range(25, 43):
        sql = next((ROOT / "questions").glob(f"q{n:02d}_*.sql")).read_text().lower()
        assert not re.search(
            r"\b(create|alter|insert|update|delete|drop|truncate|grant|revoke)\b", sql
        )
    cross = (ROOT / "questions" / "q33_experience_source_coverage.sql").read_text().lower()
    assert "store as" in cross and "reviews as" in cross and "network as" in cross
    git_scope_audit()
    assert (ROOT / "scripts" / "network_attach.py").exists() and (
        ROOT / "scripts" / "preflight.py"
    ).exists()
    print("PASS: manifest, 42 SQL shapes, privacy/network grants, Compose binding")
    print("NOTE: structural validation is NOT a PostgreSQL execution or live Metabase API test")


if __name__ == "__main__":
    try:
        check()
    except (AssertionError, SyncError, OSError, ValueError) as e:
        print("FAIL: BI structural validation " + str(e), file=sys.stderr)
        sys.exit(1)
