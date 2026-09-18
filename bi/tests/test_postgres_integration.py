"""Executable PostgreSQL SQL/authorization tests. Never use production DB."""

import copy
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
from metadata import render_sql, validate_experiment, validate_release

EXPERIMENT = validate_experiment(BI / "tests" / "fixtures" / "experiment_manifest.json")
RELEASE = validate_release(BI / "tests" / "fixtures" / "release_manifest.json")
CAPABILITIES = {
    "network_state": "NETWORK_COMPARISON_READY",
    "experiment_state": "VERIFIED_EXPERIMENT_AVAILABLE",
    "release_state": "VERIFIED_RELEASE_METADATA_AVAILABLE",
    "sentiment_state": "SENTIMENT_UNAVAILABLE",
    "topic_state": "ANNOTATIONS_UNAVAILABLE",
}


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
        # Official image can expose its bootstrap server immediately before the
        # final postmaster handoff; avoid racing that isolated-container restart.
        import time

        time.sleep(2)

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


def rows(pg, n, replace=None, experiment=EXPERIMENT, release=RELEASE, capabilities=CAPABILITIES):
    file = next((BI / "questions").glob(f"q{n:02d}_*.sql"))
    sql = render_sql(file.read_text(), experiment, release, capabilities)
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
    for n in list(range(1, 13)) + list(range(18, 43)):
        assert isinstance(rows(pg, n), list), f"Q{n:02} did not execute as reader"


def test_network_release_and_cross_domain_values(pg):
    readiness = rows(pg, 25)[0]
    assert readiness["manifest_capture_count"] == 9
    assert readiness["network_state"] == "NETWORK_COMPARISON_READY"
    coverage = rows(pg, 26)
    assert any(x["eligible_capture_count"] >= 3 for x in coverage)
    trials = rows(pg, 27, {"application": "ir.android.baham"})
    assert len(trials) == 6 and all(x["throughput_eligibility"] == "eligible" for x in trials)
    assert all(float(x["total_transfer_amplification_ratio"]) == 1.1 for x in rows(pg, 28))
    assert all(
        float(x["tcp_retransmitted_payload_share_recovery_tax"]) == 0.005 for x in rows(pg, 29)
    )
    assert all(x["protocol_capability_status"] == "tcp_observed" for x in rows(pg, 30))
    summary = rows(pg, 31)
    baham_before = next(
        x
        for x in summary
        if x["application_package"] == "ir.android.baham"
        and x["experiment_id"] == "11111111-1111-4111-8111-111111111111"
    )
    assert baham_before["n_valid"] == 3
    assert float(baham_before["median_per_capture_effective_file_throughput_mbps"]) == 20
    assert baham_before["comparison_status"] == "descriptive_only_3_to_5"
    paired = rows(pg, 32)
    assert len(paired) == 3 and all(
        x["pair_status"] == "descriptive_paired_observation" for x in paired
    )
    assert len(rows(pg, 33)) >= 2 and len(rows(pg, 34)) >= 1
    assert len(rows(pg, 35)) >= 1 and len(rows(pg, 36)) >= 3
    assert rows(pg, 37)[0]["release_readiness"] == "release_metadata_ready_observational_only"
    release_net = rows(pg, 38)[0]
    assert release_net["before_n"] == 3 and release_net["after_n"] == 3
    assert float(release_net["effective_mbps_after_minus_before"]) == 5
    store_release = rows(pg, 39)
    assert any(
        x["before_observed_days"] >= 1 and x["after_observed_days"] >= 1 for x in store_release
    )
    user_voice = rows(pg, 40)
    assert user_voice and user_voice[0]["before_classified_sentiment_reviews"] is None
    assert rows(pg, 41)[0]["network_before_n"] == 3
    assert "network_complaint_topics_unavailable" in rows(pg, 42)[0]["exclusions"]


def test_condition_metric_release_and_historical_regressions(pg):
    _, admin = pg
    split_conditions = copy.deepcopy(EXPERIMENT)
    split_conditions.records = [
        copy.deepcopy(row)
        for row in EXPERIMENT.records
        if row["application_package"] == "ir.android.baham"
        and row["experiment_id"] == "11111111-1111-4111-8111-111111111111"
    ]
    for index, row in enumerate(split_conditions.records, 1):
        row["pair_id"] = None
        row["comparison_cohort_id"] = None
        row["device_model"] = f"Synthetic Device {index}"
    split = rows(pg, 31, experiment=split_conditions)
    assert len(split) == 3
    assert all(row["n_valid"] == 1 for row in split)
    assert all(row["median_per_capture_effective_file_throughput_mbps"] is None for row in split)

    followup_ids = [
        "10000000-0000-0000-0000-000000000004",
        "10000000-0000-0000-0000-000000000005",
        "10000000-0000-0000-0000-000000000006",
    ]
    quoted = ",".join("'" + value + "'" for value in followup_ids)
    mixed_scenarios = copy.deepcopy(EXPERIMENT)
    for row in mixed_scenarios.records:
        if row["capture_id"] in followup_ids:
            row["scenario"] = "download"
            row["experiment_id"] = "11111111-1111-4111-8111-111111111111"
            row["app_version"] = "1.0"
            row["experiment_phase"] = "baseline"
    try:
        admin(
            "sahabino",
            f"UPDATE network_captures SET scenario='download' WHERE id IN ({quoted});"
            f"UPDATE network_analysis_results SET scenario='download' WHERE capture_id IN ({quoted});",
        )
        condition_rows = [
            row
            for row in rows(pg, 36, experiment=mixed_scenarios)
            if row["package_name"] == "ir.android.baham" and row["scenario"]
        ]
        assert {row["scenario"] for row in condition_rows} == {"upload", "download"}
        assert all(row["comparison_ready_n"] == 3 for row in condition_rows)
    finally:
        admin(
            "sahabino",
            f"UPDATE network_captures SET scenario='upload' WHERE id IN ({quoted});"
            f"UPDATE network_analysis_results SET scenario='upload' WHERE capture_id IN ({quoted});",
        )

    ineligible_ids = [
        "10000000-0000-0000-0000-000000000002",
        "10000000-0000-0000-0000-000000000003",
        "10000000-0000-0000-0000-000000000005",
        "10000000-0000-0000-0000-000000000006",
    ]
    ineligible_quoted = ",".join("'" + value + "'" for value in ineligible_ids)
    try:
        admin(
            "sahabino",
            f"UPDATE network_captures SET transfer_file_size_bytes=999999 WHERE id IN ({ineligible_quoted});",
        )
        gated = rows(pg, 38)[0]
        assert gated["before_observed_n"] == 3 and gated["after_observed_n"] == 3
        for metric in ("throughput", "amplification", "recovery"):
            assert gated[f"before_{metric}_eligible_n"] == 1
            assert gated[f"after_{metric}_eligible_n"] == 1
        assert gated["effective_mbps_after_minus_before"] is None
        assert gated["amplification_after_minus_before"] is None
        assert gated["tcp_recovery_tax_after_minus_before"] is None
        assert (
            rows(pg, 41)[0]["evidence_matrix_status"]
            != "multi_source_descriptive_evidence_available"
        )
    finally:
        admin(
            "sahabino",
            f"UPDATE network_captures SET transfer_file_size_bytes=1000000 WHERE id IN ({ineligible_quoted});",
        )

    absent = copy.deepcopy(EXPERIMENT)
    absent.records = []
    for index, row in enumerate(EXPERIMENT.records[:6], 1):
        clone = copy.deepcopy(row)
        clone["capture_id"] = str(uuid.UUID(int=0x70000000000040008000000000000000 + index))
        absent.records.append(clone)
    no_analysis = rows(pg, 41, experiment=absent)[0]
    assert no_analysis["network_before_manifest_n"] == 3
    assert no_analysis["network_before_observed_n"] == 0
    assert no_analysis["network_before_analyzed_n"] == 0
    assert no_analysis["before_throughput_eligible_n"] == 0
    assert no_analysis["evidence_matrix_status"] != "multi_source_descriptive_evidence_available"

    early_cutoff = copy.deepcopy(RELEASE)
    early_cutoff.records[0]["baseline_end"] = early_cutoff.records[0]["baseline_start"]
    historical = rows(pg, 40, release=early_cutoff)[0]
    assert abs(float(historical["before_mean_sampled_review_stars"]) - (8 / 3)) < 0.000001
    cohort_day = next(
        row
        for row in rows(pg, 35)
        if row["package_name"] == "ir.android.baham" and row["utc_day"] == "2026-09-01"
    )
    assert abs(float(cohort_day["mean_sampled_review_stars_1_to_5"]) - (8 / 3)) < 0.000001


def test_q41_network_optional_and_historical_locale_gate(pg):
    _, admin = pg
    no_schema_capabilities = {**CAPABILITIES, "network_state": "NETWORK_SCHEMA_MISSING"}
    try:
        admin(
            "sahabino",
            "ALTER TABLE public.network_analysis_results RENAME TO network_analysis_results_hidden; ALTER TABLE public.network_captures RENAME TO network_captures_hidden;",
        )
        base_rows = rows(pg, 41, capabilities=no_schema_capabilities)
        assert base_rows
        assert all(row["network_before_n"] is None for row in base_rows)
    finally:
        admin(
            "sahabino",
            "ALTER TABLE public.network_captures_hidden RENAME TO network_captures; ALTER TABLE public.network_analysis_results_hidden RENAME TO network_analysis_results;",
        )

    revoke_columns = """
DO $block$ DECLARE item record; BEGIN
 FOR item IN SELECT table_name,string_agg(quote_ident(column_name),',' ORDER BY ordinal_position) cols
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name IN('network_captures','network_analysis_results')
  GROUP BY table_name
 LOOP
  EXECUTE format('REVOKE SELECT (%s) ON public.%I FROM sahabino_bi_reader',item.cols,item.table_name);
 END LOOP;
END $block$;
"""
    try:
        admin("sahabino", revoke_columns)
        no_grants = rows(
            pg, 41, capabilities={**CAPABILITIES, "network_state": "NETWORK_GRANTS_MISSING"}
        )
        assert no_grants and all(row["network_before_n"] is None for row in no_grants)
    finally:
        admin(
            "postgres",
            (BI / "provisioning" / "roles_and_grants.sql.example").read_text(),
        )

    same_locale = next(
        row
        for row in rows(pg, 41)
        if row["store_country_code"] == "IR" and row["store_language_code"] == "fa"
    )
    assert same_locale["store_before_days"] > 0 and same_locale["store_after_days"] > 0
    assert same_locale["store_locale_status"] == "store_same_locale_ready"

    try:
        admin(
            "sahabino",
            "UPDATE crawl_tasks SET country_code='US',language_code='en' WHERE id IN "
            "('00000000-0000-0000-0000-000000001004','00000000-0000-0000-0000-000000001005',"
            "'00000000-0000-0000-0000-000000001014');"
            "UPDATE crawl_tasks SET country_code='GB',language_code='en' WHERE id="
            "'00000000-0000-0000-0000-000000001013';",
        )
        mismatched = rows(pg, 41)
        assert mismatched
        assert all(row["store_locale_status"] != "store_same_locale_ready" for row in mismatched)
        assert all(
            row["evidence_matrix_status"] != "multi_source_descriptive_evidence_available"
            for row in mismatched
        )
    finally:
        admin(
            "sahabino",
            "UPDATE crawl_tasks SET country_code='IR',language_code='fa' WHERE id IN "
            "('00000000-0000-0000-0000-000000001004','00000000-0000-0000-0000-000000001005',"
            "'00000000-0000-0000-0000-000000001014');"
            "UPDATE crawl_tasks SET country_code='US',language_code='en' WHERE id="
            "'00000000-0000-0000-0000-000000001013';",
        )

    try:
        admin(
            "sahabino",
            "INSERT INTO crawl_tasks VALUES "
            "('00000000-0000-0000-0000-000000002099','00000000-0000-0000-0000-000000000001',"
            "'reviews','succeeded','en','US');"
            "INSERT INTO reviews(id,application_id,external_review_id,source_at,author_name,score,content,"
            "source_adapter,first_observed_at,last_observed_at) VALUES "
            "(199,'00000000-0000-0000-0000-000000000001','private-199','2026-09-05 10:00Z',"
            "'PRIVATE',4,'SECRET TEXT','synthetic','2026-09-05 10:00Z','2026-09-05 10:00Z');"
            "INSERT INTO review_observations VALUES "
            "('00000000-0000-0000-0000-000000002099',199,'2026-09-05 10:00Z',1,4,0,'synthetic');",
        )
        incompatible = next(
            row
            for row in rows(pg, 41)
            if row["store_country_code"] == "IR" and row["store_language_code"] == "fa"
        )
        assert incompatible["review_after_n"] is None
        assert incompatible["store_review_locale_status"] == "same_locale_review_cohort_missing"
        admin(
            "sahabino",
            "UPDATE crawl_tasks SET country_code='IR',language_code='fa' WHERE id="
            "'00000000-0000-0000-0000-000000002099';",
        )
        compatible = next(
            row
            for row in rows(pg, 41)
            if row["store_country_code"] == "IR" and row["store_language_code"] == "fa"
        )
        assert compatible["review_before_n"] > 0 and compatible["review_after_n"] == 1
        assert compatible["store_review_locale_status"] == "same_locale_review_cohort_ready"
        assert compatible["evidence_matrix_status"] == "multi_source_descriptive_evidence_available"
    finally:
        admin(
            "sahabino",
            "DELETE FROM review_observations WHERE review_id=199; DELETE FROM reviews WHERE id=199; DELETE FROM crawl_tasks WHERE id='00000000-0000-0000-0000-000000002099';",
        )


def test_network_sensitive_columns_and_write_denied(pg):
    assert (
        reader(pg, "SELECT object_key FROM public.network_captures;", check=False).returncode != 0
    )
    assert (
        reader(pg, "SELECT original_filename FROM public.network_captures;", check=False).returncode
        != 0
    )
    assert (
        reader(pg, "SELECT error_message FROM public.network_captures;", check=False).returncode
        != 0
    )
    assert (
        reader(
            pg, "SELECT analysis_warnings FROM public.network_analysis_results;", check=False
        ).returncode
        != 0
    )
    assert reader(pg, "SELECT * FROM public.network_captures;", check=False).returncode != 0
    assert (
        reader(
            pg, "UPDATE public.network_captures SET status='failed' WHERE false;", check=False
        ).returncode
        != 0
    )


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
    release_voice = rows(
        pg, 40, capabilities={**CAPABILITIES, "sentiment_state": "SENTIMENT_DATA_AVAILABLE"}
    )
    assert sum(x["before_classified_sentiment_reviews"] for x in release_voice) == 2
    assert sum(x["before_historical_sentiment_unavailable_reviews"] for x in release_voice) == 1
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
