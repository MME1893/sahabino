#!/usr/bin/env python3
"""Offline structural safety check ONLY; never substitutes for PostgreSQL or API integration."""

import re
import sys
from pathlib import Path

from metabase_sync import DML, ROOT, SyncError, load_manifest, render_unfiltered


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
    assert len(manifest["questions"]) == 24 and len(manifest["dashboards"]) == 4
    files = {p.name for p in (ROOT / "questions").glob("*.sql")}
    assert files == {Path(q["sql"]).name for q in manifest["questions"]}
    for q in manifest["questions"]:
        sql = (ROOT / q["sql"]).read_text()
        clean = re.sub(r"/\*.*?\*/|--[^\n]*", " ", render_unfiltered(sql), flags=re.S)
        assert clean.strip().upper().startswith(("SELECT", "WITH")) and not DML.search(clean)
        assert "reviews.content" not in sql and "review_observations.content" not in sql
    assert (ROOT / "scripts" / "network_attach.py").exists() and (
        ROOT / "scripts" / "preflight.py"
    ).exists()
    print("PASS: manifest, 24 SQL shapes, privacy grants, Compose digest/binding, helper files")
    print("NOTE: structural validation is NOT a PostgreSQL execution or live Metabase API test")


if __name__ == "__main__":
    try:
        check()
    except (AssertionError, SyncError, OSError, ValueError) as e:
        print("FAIL: BI structural validation " + str(e), file=sys.stderr)
        sys.exit(1)
