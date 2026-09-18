#!/usr/bin/env python3
"""Stdlib-only disposable PostgreSQL validation; never connects to an existing DB."""

from __future__ import annotations

import json
import copy
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
from metabase_sync import render_unfiltered
from metadata import render_sql, validate_experiment, validate_release
from preflight import privilege_sql

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
    result = subprocess.run(
        ["docker", *args], input=stdin, text=True, capture_output=True, timeout=90
    )
    if check and result.returncode:
        raise RuntimeError(
            "Disposable Docker/PostgreSQL command failed at "
            + " ".join(args[:6])
            + ": "
            + result.stderr[-1000:]
        )
    return result


def main():
    name = "sahabino-bi-validation-" + uuid.uuid4().hex[:12]
    if not name.startswith("sahabino-bi-validation-"):
        raise RuntimeError("unsafe disposable container name")
    started = False
    try:
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
            print("NOT RUN: disposable postgres:16-alpine could not start", file=sys.stderr)
            return 77
        started = True
        for _ in range(30):
            if (
                docker(
                    "exec", name, "pg_isready", "-U", "postgres", "-d", "sahabino", check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("disposable PostgreSQL readiness timed out")
        # The official image briefly exposes its bootstrap server before the
        # final postmaster restart; wait through that isolated-container handoff.
        time.sleep(2)

        def psql(db, sql, user="postgres", check=True):
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
                user,
                "-d",
                db,
                stdin=sql,
                check=check,
            )

        psql("sahabino", (BI / "tests" / "schema.sql").read_text())
        psql("sahabino", (BI / "tests" / "seed.sql").read_text())
        psql(
            "postgres",
            "REVOKE CONNECT,TEMPORARY ON DATABASE postgres FROM PUBLIC; REVOKE CONNECT,TEMPORARY ON DATABASE sahabino FROM PUBLIC;",
        )
        grants = (BI / "provisioning" / "roles_and_grants.sql.example").read_text()
        psql("postgres", grants)
        psql("postgres", grants)
        audit = json.loads(psql("sahabino", privilege_sql(), "sahabino_bi_reader").stdout.strip())
        assert audit["missing_network"] == 0 and audit["denied_network"] == 0
        assert audit["forbidden_column_access"] == 0 and audit["writable_tables"] == 0

        def question(
            number, replacements=None, capabilities=None, experiment=EXPERIMENT, release=RELEASE
        ):
            path = next((BI / "questions").glob(f"q{number:02d}_*.sql"))
            sql = render_sql(path.read_text(), experiment, release, capabilities or CAPABILITIES)
            if replacements:
                for key, value in replacements.items():
                    pattern = r"\[\[([^\[\]]*\{\{" + re.escape(key) + r"\}\}[^\[\]]*)\]\]"
                    sql = re.sub(
                        pattern,
                        lambda match: match.group(1).replace(
                            "{{" + key + "}}", "'" + value.replace("'", "''") + "'"
                        ),
                        sql,
                    )
                sql = re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S)
            else:
                sql = render_unfiltered(sql)
            wrapped = "SELECT row_to_json(q)::text FROM (" + sql.strip().removesuffix(";") + ") q;"
            out = psql("sahabino", wrapped, "sahabino_bi_reader").stdout
            return [json.loads(line) for line in out.splitlines() if line]

        for number in list(range(1, 13)) + list(range(18, 43)):
            assert isinstance(question(number), list)
        assert question(25)[0]["manifest_capture_count"] == 9
        summary = question(31)
        before = next(
            row
            for row in summary
            if row["application_package"] == "ir.android.baham"
            and row["experiment_id"] == "11111111-1111-4111-8111-111111111111"
        )
        assert before["n_valid"] == 3
        assert float(before["median_per_capture_effective_file_throughput_mbps"]) == 20
        assert len(question(32)) == 3
        release_net = question(38)[0]
        assert (release_net["before_n"], release_net["after_n"]) == (3, 3)
        assert float(release_net["effective_mbps_after_minus_before"]) == 5
        assert question(37)[0]["release_readiness"] == "release_metadata_ready_observational_only"
        assert question(40)[0]["before_classified_sentiment_reviews"] is None
        assert "network_complaint_topics_unavailable" in question(42)[0]["exclusions"]
        assert len(question(27, {"application": "ir.android.baham"})) == 6

        # Three otherwise eligible trials on different devices are three groups of one,
        # never one benchmark group of three.
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
        split = question(31, experiment=split_conditions)
        assert len(split) == 3
        assert all(row["n_valid"] == 1 for row in split)
        assert all(
            row["median_per_capture_effective_file_throughput_mbps"] is None for row in split
        )

        # Make the follow-up captures genuine downloads in the disposable DB and
        # prove Q36 retains separate scenario/condition rows rather than pooling.
        followup_ids = [
            "10000000-0000-0000-0000-000000000004",
            "10000000-0000-0000-0000-000000000005",
            "10000000-0000-0000-0000-000000000006",
        ]
        quoted = ",".join("'" + value + "'" for value in followup_ids)
        psql(
            "sahabino",
            f"UPDATE network_captures SET scenario='download' WHERE id IN ({quoted});"
            f"UPDATE network_analysis_results SET scenario='download' WHERE capture_id IN ({quoted});",
        )
        mixed_scenarios = copy.deepcopy(EXPERIMENT)
        for row in mixed_scenarios.records:
            if row["capture_id"] in followup_ids:
                row["scenario"] = "download"
                row["experiment_id"] = "11111111-1111-4111-8111-111111111111"
                row["app_version"] = "1.0"
                row["experiment_phase"] = "baseline"
        condition_rows = [
            row
            for row in question(36, experiment=mixed_scenarios)
            if row["package_name"] == "ir.android.baham" and row["scenario"]
        ]
        assert {row["scenario"] for row in condition_rows} == {"upload", "download"}
        assert all(row["comparison_ready_n"] == 3 for row in condition_rows)
        psql(
            "sahabino",
            f"UPDATE network_captures SET scenario='upload' WHERE id IN ({quoted});"
            f"UPDATE network_analysis_results SET scenario='upload' WHERE capture_id IN ({quoted});",
        )

        # One eligible and two size-mismatched captures in each release period:
        # observed evidence remains visible, but every primary delta is NULL.
        ineligible_ids = [
            "10000000-0000-0000-0000-000000000002",
            "10000000-0000-0000-0000-000000000003",
            "10000000-0000-0000-0000-000000000005",
            "10000000-0000-0000-0000-000000000006",
        ]
        ineligible_quoted = ",".join("'" + value + "'" for value in ineligible_ids)
        psql(
            "sahabino",
            f"UPDATE network_captures SET transfer_file_size_bytes=999999 WHERE id IN ({ineligible_quoted});",
        )
        gated = question(38)[0]
        assert gated["before_observed_n"] == 3 and gated["after_observed_n"] == 3
        assert (
            gated["before_throughput_eligible_n"] == 1 and gated["after_throughput_eligible_n"] == 1
        )
        assert (
            gated["before_amplification_eligible_n"] == 1
            and gated["after_amplification_eligible_n"] == 1
        )
        assert gated["before_recovery_eligible_n"] == 1 and gated["after_recovery_eligible_n"] == 1
        assert gated["effective_mbps_after_minus_before"] is None
        assert gated["amplification_after_minus_before"] is None
        assert gated["tcp_recovery_tax_after_minus_before"] is None
        assert question(41)[0]["evidence_matrix_status"] == "network_metric_period_insufficient"
        psql(
            "sahabino",
            f"UPDATE network_captures SET transfer_file_size_bytes=1000000 WHERE id IN ({ineligible_quoted});",
        )

        # Manifest rows without analyzed source records never create release readiness.
        absent = copy.deepcopy(EXPERIMENT)
        absent.records = []
        for index, row in enumerate(EXPERIMENT.records[:6], 1):
            clone = copy.deepcopy(row)
            clone["capture_id"] = str(uuid.UUID(int=0x70000000000040008000000000000000 + index))
            absent.records.append(clone)
        no_analysis = question(41, experiment=absent)[0]
        assert no_analysis["network_before_manifest_n"] == 3
        assert no_analysis["network_before_observed_n"] == 0
        assert no_analysis["network_before_analyzed_n"] == 0
        assert no_analysis["before_throughput_eligible_n"] == 0
        assert (
            no_analysis["evidence_matrix_status"] != "multi_source_descriptive_evidence_available"
        )

        # A score revision after an explicit baseline cutoff cannot rewrite baseline.
        early_cutoff = copy.deepcopy(RELEASE)
        early_cutoff.records[0]["baseline_end"] = early_cutoff.records[0]["baseline_start"]
        historical = question(40, release=early_cutoff)[0]
        assert abs(float(historical["before_mean_sampled_review_stars"]) - (8 / 3)) < 0.000001
        cohort_day = next(
            row
            for row in question(35)
            if row["package_name"] == "ir.android.baham" and row["utc_day"] == "2026-09-01"
        )
        assert abs(float(cohort_day["mean_sampled_review_stars_1_to_5"]) - (8 / 3)) < 0.000001

        denied = [
            "SELECT content FROM public.reviews;",
            "SELECT object_key FROM public.network_captures;",
            "SELECT original_filename FROM public.network_captures;",
            "SELECT error_message FROM public.network_captures;",
            "SELECT analysis_warnings FROM public.network_analysis_results;",
            "SELECT * FROM public.network_captures;",
            "UPDATE public.network_captures SET status='failed' WHERE false;",
            "CREATE TABLE public.bi_should_not_exist(id int);",
        ]
        assert all(
            psql("sahabino", sql, "sahabino_bi_reader", check=False).returncode != 0
            for sql in denied
        )

        psql(
            "sahabino",
            """
ALTER TABLE public.review_observations ADD COLUMN content text;
ALTER TABLE public.review_observations ADD COLUMN source_at timestamptz;
ALTER TABLE public.review_observations ADD COLUMN sentiment_language text;
ALTER TABLE public.review_observations ADD COLUMN sentiment_label text;
ALTER TABLE public.review_observations ADD COLUMN sentiment_status text NOT NULL DEFAULT 'skipped';
ALTER TABLE public.review_observations ADD COLUMN sentiment_processed_at timestamptz;
ALTER TABLE public.review_observations ADD COLUMN sentiment_attempt_count smallint NOT NULL DEFAULT 0;
UPDATE public.review_observations SET sentiment_status='done',sentiment_label='positive',sentiment_language='fa' WHERE review_id=101 AND observed_at='2026-09-03 10:00Z';
UPDATE public.review_observations SET sentiment_status='done',sentiment_label='positive',sentiment_language='en' WHERE review_id=103;
""",
        )
        psql("postgres", grants)
        for number in range(13, 18):
            assert isinstance(question(number), list)
        sentiment_capabilities = {**CAPABILITIES, "sentiment_state": "SENTIMENT_DATA_AVAILABLE"}
        release_voice = question(40, capabilities=sentiment_capabilities)
        assert sum(row["before_classified_sentiment_reviews"] for row in release_voice) == 2
        assert (
            sum(row["before_historical_sentiment_unavailable_reviews"] for row in release_voice)
            == 1
        )
        assert sum(row["after_classified_sentiment_reviews"] for row in release_voice) == 0
        print(
            "PASS: disposable PostgreSQL executed Q01-Q42 as restricted reader with value/privacy assertions"
        )
        return 0
    finally:
        if started and name.startswith("sahabino-bi-validation-"):
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("FAIL: disposable PostgreSQL validation: " + str(exc), file=sys.stderr)
        sys.exit(1)
