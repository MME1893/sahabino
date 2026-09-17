"""Executable PostgreSQL SQL/authorization tests. Never use production DB."""

import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
from metabase_sync import render_unfiltered


def docker(*args, stdin=None, check=True):
    return subprocess.run(
        ["docker", *args], input=stdin, text=True, capture_output=True, check=check, timeout=60
    )


@pytest.fixture(scope="module")
def pg():
    if not shutil.which("docker"):
        pytest.skip("NOT RUN: Docker unavailable; disposable PostgreSQL could not be started")
    name = "sahabino-bi-test-" + uuid.uuid4().hex[:12]
    try:
        # No published ports and no network; local socket trust inside disposable isolated DB only.
        result = docker(
            "run",
            "-d",
            "--rm",
            "--network",
            "none",
            "--name",
            name,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_DB=sahabino",
            "postgres:16-alpine",
            check=False,
        )
        if result.returncode:
            pytest.skip("NOT RUN: disposable postgres:16-alpine Docker image/daemon unavailable")
        for i in range(30):
            if (
                docker(
                    "exec", name, "pg_isready", "-U", "postgres", "-d", "sahabino", check=False
                ).returncode
                == 0
            ):
                break
            import time

            time.sleep(1)
        else:
            pytest.fail("Disposable PostgreSQL readiness timed out")

        def admin(db, sql):
            return docker(
                "exec",
                "-i",
                name,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                db,
                stdin=sql,
            )

        admin("sahabino", (BI / "tests" / "schema.sql").read_text())
        admin("sahabino", (BI / "tests" / "seed.sql").read_text())
        # Disposable-only hardening: do NOT perform broad PUBLIC revokes on production.
        # Without this, default PUBLIC TEMP makes metabase_app writable in other DBs.
        admin(
            "postgres",
            "REVOKE CONNECT,TEMPORARY ON DATABASE postgres FROM PUBLIC; REVOKE CONNECT,TEMPORARY ON DATABASE sahabino FROM PUBLIC;",
        )
        admin("postgres", (BI / "provisioning" / "roles_and_grants.sql.example").read_text())
        # second application tests role/database idempotency and complete reader re-grant
        admin("postgres", (BI / "provisioning" / "roles_and_grants.sql.example").read_text())
        yield name, admin
    finally:
        docker("rm", "-f", name, check=False)


def reader(pg, sql, check=True):
    name, _ = pg
    return docker(
        "exec",
        "-i",
        name,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-A",
        "-t",
        "-U",
        "sahabino_bi_reader",
        "-d",
        "sahabino",
        stdin=sql,
        check=check,
    )


def rows(pg, n, replace=None):
    file = next((BI / "questions").glob(f"q{n:02d}_*.sql"))
    sql = file.read_text()
    if replace:
        for key, val in replace.items():
            pattern = r"\[\[([^\[\]]*\{\{" + re.escape(key) + r"\}\}[^\[\]]*)\]\]"
            sql = re.sub(
                pattern,
                lambda match: match.group(1).replace(
                    "{{" + key + "}}", "'" + val.replace("'", "''") + "'"
                ),
                sql,
            )
    sql = (
        render_unfiltered(sql).strip().removesuffix(";")
        if not replace
        else re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S).strip().removesuffix(";")
    )
    out = reader(pg, "SELECT row_to_json(q)::text FROM (" + sql + ") q;").stdout
    return [json.loads(line) for line in out.splitlines() if line]


def test_every_question_base_as_reader(pg):
    for n in list(range(1, 13)) + list(range(18, 25)):
        assert isinstance(rows(pg, n), list), f"Q{n:02} did not execute as reader"


def test_store_grains_fixture_locale_precision_and_changes(pg):
    q1 = rows(pg, 1)
    assert len(q1) == 7 and all(x["package_name"] != "ir.rightel.myrightel" for x in q1)
    assert sum(not x["has_successful_snapshot"] for x in q1) == 1
    q2 = rows(pg, 2)
    keys = [
        (
            x["package_name"],
            x["crawl_country_code"],
            x["crawl_language_code"],
            x["snapshot_day_utc"],
        )
        for x in q2
    ]
    assert len(keys) == len(set(keys))
    assert not any(x["package_name"] == "ir.rightel.myrightel" for x in q2)
    baham = [
        x for x in q2 if x["package_name"] == "ir.android.baham" and x["crawl_country_code"] == "IR"
    ]
    assert [x["snapshot_day_utc"] for x in baham] == [
        "2026-09-01",
        "2026-09-03",
        "2026-09-05",
        "2026-09-07",
    ]
    assert abs(float(baham[0]["store_score_0_to_5"]) - 4.1234567) < 0.00000001
    assert baham[0]["snapshots_in_day"] == 2
    assert any(
        x["crawl_country_code"] == "US" and x["crawl_language_code"] == "en"
        for x in q2
        if x["package_name"] == "ir.android.baham"
    )
    q3 = rows(pg, 3)
    assert len(q3) == len(
        {(x["package_name"], x["crawl_country_code"], x["crawl_language_code"]) for x in q3}
    )
    p = next(
        x for x in q3 if x["package_name"] == "ir.android.baham" and x["crawl_country_code"] == "IR"
    )
    assert (
        p["peer_app_count"] == 1
        and p["peer_benchmark_status"] == "insufficient_one_peer"
        and p["peer_median_score_0_to_5"] is None
    )
    assert any(
        x["peer_app_count"] == 3 and x["peer_benchmark_status"] == "three_plus_peers" for x in q3
    )
    q4 = rows(pg, 4)
    assert len(q4) == len(
        {
            (
                x["package_name"],
                x["crawl_country_code"],
                x["crawl_language_code"],
                x["snapshot_day_utc"],
            )
            for x in q4
        }
    )
    neg = next(
        x
        for x in q4
        if x["package_name"] == "ir.android.baham" and x["snapshot_day_utc"] == "2026-09-03"
    )
    assert (
        neg["ratings_count_change"] == -10
        and neg["reviews_count_change"] == -5
        and neg["days_since_previous_observation"] == 2
    )
    assert (
        next(
            x
            for x in q4
            if x["package_name"] == "ir.android.baham"
            and x["snapshot_day_utc"] == "2026-09-01"
            and x["crawl_country_code"] == "IR"
        )["ratings_count_change"]
        is None
    )
    q5 = rows(pg, 5)
    assert len(q5) == len(
        {
            (
                x["package_name"],
                x["crawl_country_code"],
                x["crawl_language_code"],
                x["snapshot_day_utc"],
            )
            for x in q5
        }
    )
    v = {
        x["snapshot_day_utc"]: x
        for x in q5
        if x["package_name"] == "ir.android.baham" and x["crawl_country_code"] == "IR"
    }
    assert v["2026-09-03"]["threshold_observation_status"] == "unchanged"
    assert v["2026-09-05"]["newly_observed_higher_threshold"] == 5000
    assert v["2026-09-07"]["newly_observed_higher_threshold"] is None
    matched = rows(pg, 18)
    assert len(matched) >= 1
    assert len(matched) == len(
        {
            (x["snapshot_day_utc"], x["crawl_country_code"], x["crawl_language_code"])
            for x in matched
        }
    )
    assert any(x["observed_ratings_count_change"] == -10 for x in rows(pg, 19))
    assert any(x["observed_reviews_count_change"] == -5 for x in rows(pg, 20))
    assert all(
        x["min_installs_threshold"] is None or x["min_installs_threshold"] >= 0
        for x in rows(pg, 21)
    )
    assert len(rows(pg, 22)) >= 1
    assert sum(x["newly_observed_unique_reviews"] for x in rows(pg, 23)) == 3
    assert (
        rows(pg, 23, {"country": "US"}) == []
    )  # US observation of review 103 is not the globally first encounter
    assert isinstance(rows(pg, 24), list)


def test_filters_and_lag_baseline(pg):
    q4 = rows(
        pg,
        4,
        {
            "application": "ir.android.baham",
            "start_date": "2026-09-03",
            "end_date": "2026-09-03",
            "country": "IR",
        },
    )
    assert (
        len(q4) == 1
        and q4[0]["ratings_count_change"] == -10
        and q4[0]["previous_observation_day_utc"] == "2026-09-01"
    )
    q3 = rows(pg, 3, {"application": "ir.android.baham"})
    assert q3[0]["peer_app_count"] == 1  # focal filter applied AFTER peer cohort
    q2 = rows(pg, 2, {"application": "app.pinno", "country": "IR", "language": "fa"})
    assert len(q2) == 3 and all(x["package_name"] == "app.pinno" for x in q2)


def test_base_reviews_revision_and_privacy(pg):
    assert reader(pg, "SELECT content FROM public.reviews;", check=False).returncode != 0
    assert reader(pg, "SELECT external_review_id FROM public.reviews;", check=False).returncode != 0
    assert reader(pg, "SELECT author_name FROM public.reviews;", check=False).returncode != 0
    assert reader(pg, "UPDATE public.reviews SET score=5 WHERE false;", check=False).returncode != 0
    assert (
        reader(pg, "CREATE TABLE public.bi_should_not_exist(id int);", check=False).returncode != 0
    )
    assert reader(pg, "SELECT * FROM public.reviews;", check=False).returncode != 0
    star = rows(pg, 10)
    assert sum(x["unique_review_count"] for x in star) == 3
    cohort = rows(pg, 11)
    assert sum(x["unique_review_count"] for x in cohort) == 3
    ir = next(x for x in cohort if x["crawl_country_code"] == "IR")
    assert ir["unique_review_count"] == 2 and float(ir["mean_unique_review_stars_1_to_5"]) == 3
    assert abs(float(ir["negative_review_share"]) - 0.5) < 0.00001
    revisions = rows(pg, 12)
    assert sum(x["score_revision_count"] for x in revisions) == 1
    assert sum(x["score_upgrade_count"] for x in revisions) == 1
    assert sum(x["score_downgrade_count"] for x in revisions) == 0
    assert not rows(
        pg, 12, {"country": "US"}
    )  # transition assigned to the true next observation locale
    assert (
        sum(
            x["score_revision_count"]
            for x in rows(pg, 12, {"start_date": "2026-09-03", "end_date": "2026-09-03"})
        )
        == 1
    )
    before = rows(pg, 10, {"end_date": "2026-09-01"})
    assert sum(x["unique_review_count"] for x in before) == 3
    assert next(x for x in before if x["review_stars_1_to_5"] == 1)["unique_review_count"] == 1


def test_sentiment_optional_migration_and_privacy(pg):
    _, admin = pg
    admin(
        "sahabino",
        """ALTER TABLE public.review_observations ADD COLUMN content text;
      ALTER TABLE public.review_observations ADD COLUMN source_at timestamptz;
      ALTER TABLE public.review_observations ADD COLUMN sentiment_language text;
      ALTER TABLE public.review_observations ADD COLUMN sentiment_label text;
      ALTER TABLE public.review_observations ADD COLUMN sentiment_status text NOT NULL DEFAULT 'skipped';
      ALTER TABLE public.review_observations ADD COLUMN sentiment_processed_at timestamptz;
      ALTER TABLE public.review_observations ADD COLUMN sentiment_attempt_count smallint NOT NULL DEFAULT 0;
      UPDATE public.alembic_version SET version_num='20260917_0008';
      UPDATE public.review_observations SET sentiment_status='done', sentiment_label='positive',sentiment_language='fa'
       WHERE review_id=101 AND observed_at='2026-09-03 10:00Z';
      UPDATE public.review_observations SET sentiment_status='done', sentiment_label='negative',sentiment_language='fa'
       WHERE review_id=101 AND observed_at='2026-09-01 10:00Z';
      UPDATE public.review_observations SET sentiment_status='done', sentiment_label='positive',sentiment_language='en'
       WHERE review_id=103;
      """,
    )
    admin("postgres", (BI / "provisioning" / "roles_and_grants.sql.example").read_text())
    for n in range(13, 18):
        assert isinstance(rows(pg, n), list)
    coverage = rows(pg, 13)
    assert sum(x["eligible_unique_reviews"] for x in coverage) == 3
    assert (
        sum(x["classified_unique_reviews"] for x in coverage) == 2
        and sum(x["skipped_reviews"] for x in coverage) == 1
    )
    d = rows(pg, 14)
    assert sum(x["classified_unique_reviews_for_label"] for x in d) == 2
    assert not any(
        x["sentiment_label"] == "negative" for x in d
    )  # latest review score was revised, label updated
    assert (
        reader(pg, "SELECT content FROM public.review_observations;", check=False).returncode != 0
    )
    assert (
        reader(
            pg, "SELECT sentiment_attempt_count FROM public.review_observations;", check=False
        ).returncode
        != 0
    )
    assert reader(pg, "INSERT INTO public.reviews DEFAULT VALUES;", check=False).returncode != 0


def test_public_and_membership_escalations_fail_closed(pg):
    """Disposable instance ONLY: demonstrate the grant script detects inherited/PUBLIC leaks."""
    _, admin = pg
    try:
        admin("sahabino", "GRANT SELECT(content) ON public.reviews TO PUBLIC;")
        assert reader(pg, "SELECT content FROM public.reviews;", check=False).returncode == 0
        check = docker(
            "exec",
            "-i",
            pg[0],
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            stdin=(BI / "provisioning" / "roles_and_grants.sql.example").read_text(),
            check=False,
        )
        assert check.returncode != 0 and "privilege audit failed" in (check.stderr + check.stdout)
    finally:
        admin("sahabino", "REVOKE SELECT(content) ON public.reviews FROM PUBLIC;")
    try:
        admin(
            "postgres",
            "CREATE ROLE bi_disposable_extra; GRANT bi_disposable_extra TO sahabino_bi_reader;",
        )
        check = docker(
            "exec",
            "-i",
            pg[0],
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            stdin=(BI / "provisioning" / "roles_and_grants.sql.example").read_text(),
            check=False,
        )
        assert check.returncode != 0 and "Existing memberships" in (check.stderr + check.stdout)
    finally:
        admin(
            "postgres",
            "REVOKE bi_disposable_extra FROM sahabino_bi_reader; DROP ROLE bi_disposable_extra;",
        )
    admin("postgres", (BI / "provisioning" / "roles_and_grants.sql.example").read_text())
