import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
from metadata import MetadataError, render_sql, validate_experiment, validate_release


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self):
        return json.loads((BI / "tests" / "fixtures" / "experiment_manifest.json").read_text())

    def write(self, data, name="manifest.json"):
        path = self.root / name
        path.write_text(json.dumps(data))
        return path

    def test_valid_offline_explicitly_reports_db_not_run(self):
        report = validate_experiment(BI / "tests" / "fixtures" / "experiment_manifest.json")
        self.assertTrue(report.valid)
        self.assertEqual(len(report.records), 9)
        self.assertFalse(report.db_checks_run)
        self.assertEqual(report.safe_dict()["db_check_status"], "NOT_RUN")

    def test_experiment_rejection_cases(self):
        cases = [
            (lambda p: p["captures"][0].update(capture_id="bad"), "invalid UUID"),
            (lambda p: p["captures"][0].update(experiment_id="bad"), "invalid UUID"),
            (lambda p: p["captures"][0].update(session_id="bad"), "invalid UUID"),
            (lambda p: p["captures"][0].update(pair_id="bad"), "invalid UUID"),
            (lambda p: p["captures"][0].update(scenario="browse"), "unsupported action"),
            (lambda p: p["captures"][0].update(trial_number=0), "positive integer"),
            (lambda p: p["captures"][0].update(test_file_size_bytes=0), "must be positive"),
            (lambda p: p["captures"][0].update(test_file_sha256="x"), "64 hexadecimal"),
            (lambda p: p["captures"][0].update(device_model="unknown"), "allowed_device_models"),
            (
                lambda p: p["captures"][0].update(network_profile="unknown"),
                "allowed_network_profiles",
            ),
            (
                lambda p: p["captures"][0].update(capture_started_at_utc="2026-01-01"),
                "ISO-8601 UTC",
            ),
            (
                lambda p: p["captures"][0].update(capture_finished_at_utc="2020-01-01T00:00:00Z"),
                "finish must be after",
            ),
            (lambda p: p["captures"][0].update(transfer_completed="yes"), "JSON true or false"),
            (lambda p: p["captures"][0].update(transfer_completed=False), "incomplete transfers"),
            (
                lambda p: p["captures"][0].update(capture_isolation_confirmed=False),
                "provenance is unconfirmed",
            ),
            (
                lambda p: p["captures"][0].update(cache_cleared_or_download_verified=False),
                "transfer provenance",
            ),
            (lambda p: p["captures"][0].update(validation_notes=""), "required non-empty"),
            (lambda p: p["captures"][0].update(experiment_phase="future"), "unsupported phase"),
            (lambda p: p["captures"][0].pop("protocol_notes"), "missing fields"),
            (lambda p: p["captures"].append(copy.deepcopy(p["captures"][0])), "duplicate"),
            (
                lambda p: p["captures"][0].update(validation_notes="{{application}}"),
                "reserved SQL/template",
            ),
            (
                lambda p: p["captures"][0].update(application_package="ir.rightel.myrightel"),
                "MyRightel fixture",
            ),
        ]
        for index, (mutate, expected) in enumerate(cases):
            with self.subTest(case=index):
                data = self.payload()
                mutate(data)
                report = validate_experiment(self.write(data, f"experiment-{index}.json"))
                self.assertFalse(report.valid)
                self.assertIn(expected, " ".join(report.errors))

    def test_pair_cardinality_and_conditions(self):
        data = self.payload()
        data["captures"] = data["captures"][:-1]
        data["captures"][0]["network_type"] = "cellular"
        report = validate_experiment(self.write(data))
        self.assertFalse(report.valid)
        self.assertTrue(any("comparison conditions differ" in x for x in report.errors))

    def test_cross_experiment_and_unapproved_pairs_are_rejected(self):
        data = self.payload()
        data["captures"][1]["experiment_id"] = "33333333-3333-4333-8333-333333333333"
        report = validate_experiment(self.write(data, "cross-experiment.json"))
        self.assertFalse(report.valid)
        self.assertIn("comparison conditions differ", " ".join(report.errors))

        data = self.payload()
        data["allowed_comparison_cohorts"] = [["ir.android.baham", "com.example.other"]]
        report = validate_experiment(self.write(data, "unapproved-cohort.json"))
        self.assertFalse(report.valid)
        self.assertIn("not an allowed comparison cohort", " ".join(report.errors))

    def test_db_aware_mismatch_cases(self):
        source = BI / "tests" / "fixtures" / "experiment_manifest.json"
        offline = validate_experiment(source)
        db = [
            {
                "capture_id": row["capture_id"],
                "package_name": row["application_package"],
                "scenario": row["scenario"],
                "transfer_file_size_bytes": row["test_file_size_bytes"],
                "status": "analyzed",
            }
            for row in offline.records
        ]
        self.assertTrue(validate_experiment(source, db).valid)
        bad = copy.deepcopy(db)
        bad[0]["package_name"] = "wrong.package"
        self.assertIn("attribution mismatch", " ".join(validate_experiment(source, bad).errors))
        bad = copy.deepcopy(db)
        bad[0]["scenario"] = "download"
        self.assertIn("scenario mismatch", " ".join(validate_experiment(source, bad).errors))
        bad = copy.deepcopy(db)
        bad[0]["transfer_file_size_bytes"] += 1
        self.assertIn("size mismatch", " ".join(validate_experiment(source, bad).errors))
        bad = copy.deepcopy(db)
        bad[0]["status"] = "uploaded"
        self.assertIn("incomplete", " ".join(validate_experiment(source, bad).errors))
        self.assertIn("absent", " ".join(validate_experiment(source, db[1:]).errors))

    def test_release_rejection_cases(self):
        cases = [
            ("release_event_id", "bad", "invalid UUID"),
            ("release_date", "not-date", "invalid ISO date"),
            ("release_date_precision", "hour", "expected day"),
            ("release_date_verified", "true", "JSON true or false"),
            ("baseline_start", "2026-09-05", "baseline window is reversed"),
            ("followup_start", "2026-09-03", "followup must start after"),
            ("application_package", "bad", "invalid package"),
            ("experiment_id_before", "bad", "invalid UUID"),
            ("release_date_source", "", "required non-empty"),
            ("application_package", "ir.rightel.myrightel", "MyRightel fixture"),
            ("new_version", "1.0", "must differ"),
        ]
        base = json.loads((BI / "tests" / "fixtures" / "release_manifest.json").read_text())
        for index, (field, value, expected) in enumerate(cases):
            with self.subTest(case=index):
                data = copy.deepcopy(base)
                data["release_events"][0][field] = value
                report = validate_release(self.write(data, f"release-{index}.json"))
                self.assertFalse(report.valid)
                self.assertIn(expected, " ".join(report.errors))

    def test_release_default_windows_and_safe_sql_literal(self):
        data = json.loads((BI / "tests" / "fixtures" / "release_manifest.json").read_text())
        for field in (
            "baseline_start",
            "baseline_end",
            "followup_start",
            "followup_end",
            "transition_period_start",
            "transition_period_end",
        ):
            data["release_events"][0][field] = ""
        release = validate_release(self.write(data, "release.json"))
        self.assertTrue(release.valid)
        self.assertEqual(str(release.records[0]["baseline_start"]), "2026-08-21")
        experiment = validate_experiment(BI / "tests" / "fixtures" / "experiment_manifest.json")
        private_note = "PRIVATE VALIDATION NOTE quote '); DROP TABLE x; --"
        experiment.records[0]["validation_notes"] = private_note
        release.records[0]["notes"] = "PRIVATE RELEASE NOTE"
        release.records[0]["release_evidence_reference"] = "PRIVATE-EVIDENCE-REFERENCE"
        sql = render_sql(
            (BI / "questions" / "q25_network_readiness.sql").read_text(), experiment, release, {}
        )
        self.assertNotIn(private_note, sql)
        self.assertNotIn("PRIVATE RELEASE NOTE", sql)
        self.assertNotIn("PRIVATE-EVIDENCE-REFERENCE", sql)
        self.assertNotIn(experiment.records[0]["test_file_sha256"], sql)
        self.assertIn(experiment.records[0]["file_cohort_id"], sql)
        self.assertTrue(sql.strip().endswith(";"))

    def test_overlapping_release_windows_are_rejected(self):
        data = json.loads((BI / "tests" / "fixtures" / "release_manifest.json").read_text())
        second = copy.deepcopy(data["release_events"][0])
        second["release_event_id"] = "55555555-5555-4555-8555-555555555555"
        second["previous_version"] = "1.1"
        second["new_version"] = "1.2"
        data["release_events"].append(second)
        report = validate_release(self.write(data, "overlap.json"))
        self.assertFalse(report.valid)
        self.assertIn("overlapping analysis windows", " ".join(report.errors))

    def test_unbalanced_compilation_marker_fails(self):
        with self.assertRaisesRegex(MetadataError, "unbalanced"):
            render_sql(
                "WITH x AS (/*__EXPERIMENT_ROWS_START__*/ SELECT 1) SELECT 1;",
                validate_experiment(),
                validate_release(),
                {},
            )

    def test_q41_compiles_network_references_only_when_available(self):
        source = (BI / "questions" / "q41_release_combined_evidence.sql").read_text(
            encoding="utf-8"
        )
        experiment = validate_experiment()
        release = validate_release()
        without_network = render_sql(
            source,
            experiment,
            release,
            {"network_state": "NETWORK_SCHEMA_MISSING"},
        )
        self.assertNotIn("public.network_captures", without_network)
        self.assertNotIn("public.network_analysis_results", without_network)
        with_network = render_sql(
            source,
            experiment,
            release,
            {"network_state": "NETWORK_COMPARISON_READY"},
        )
        self.assertIn("public.network_captures", with_network)
        self.assertIn("public.network_analysis_results", with_network)

        with self.assertRaisesRegex(MetadataError, "unbalanced"):
            render_sql(
                "WITH x AS (/*__OPTIONAL_NETWORK_START__*/ SELECT 1) SELECT 1;",
                experiment,
                release,
                {},
            )


if __name__ == "__main__":
    unittest.main()
