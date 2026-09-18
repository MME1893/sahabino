#!/usr/bin/env python3
"""Validate private BI metadata and compile it into typed, SELECT-only SQL CTEs."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

EXPERIMENT_FIELDS = (
    "capture_id",
    "experiment_id",
    "session_id",
    "trial_number",
    "pair_id",
    "application_package",
    "scenario",
    "capture_started_at_utc",
    "capture_finished_at_utc",
    "transfer_completed",
    "test_file_sha256",
    "test_file_size_bytes",
    "app_version",
    "device_model",
    "android_version",
    "network_type",
    "network_profile",
    "capture_tool",
    "capture_tool_version",
    "capture_isolation_confirmed",
    "cache_cleared_or_download_verified",
    "protocol_notes",
    "validation_notes",
    "experiment_phase",
)

# These are the only experiment values persisted in Metabase Saved Question SQL
# and the private synchronizer state. Raw file hashes and free-form notes remain
# validator-only inputs.
EXPERIMENT_REPORT_FIELDS = (
    "capture_id",
    "experiment_id",
    "session_id",
    "trial_number",
    "pair_id",
    "comparison_cohort_id",
    "file_cohort_id",
    "application_package",
    "scenario",
    "capture_started_at_utc",
    "capture_finished_at_utc",
    "transfer_completed",
    "test_file_size_bytes",
    "app_version",
    "device_model",
    "android_version",
    "network_type",
    "network_profile",
    "capture_tool",
    "capture_tool_version",
    "capture_isolation_confirmed",
    "cache_cleared_or_download_verified",
    "experiment_phase",
)

RELEASE_FIELDS = (
    "release_event_id",
    "application_package",
    "previous_version",
    "new_version",
    "release_date",
    "release_date_precision",
    "release_date_source",
    "release_evidence_reference",
    "release_date_verified",
    "baseline_start",
    "baseline_end",
    "followup_start",
    "followup_end",
    "transition_period_start",
    "transition_period_end",
    "experiment_id_before",
    "experiment_id_after",
    "notes",
)

RELEASE_REPORT_FIELDS = (
    "release_event_id",
    "application_package",
    "previous_version",
    "new_version",
    "release_date",
    "release_date_precision",
    "release_date_verified",
    "baseline_start",
    "baseline_end",
    "followup_start",
    "followup_end",
    "transition_period_start",
    "transition_period_end",
    "experiment_id_before",
    "experiment_id_after",
)

EXP_MARKERS = ("/*__EXPERIMENT_ROWS_START__*/", "/*__EXPERIMENT_ROWS_END__*/")
REL_MARKERS = ("/*__RELEASE_ROWS_START__*/", "/*__RELEASE_ROWS_END__*/")
CAP_MARKERS = ("/*__CAPABILITY_ROWS_START__*/", "/*__CAPABILITY_ROWS_END__*/")
SENT_MARKERS = ("/*__SENTIMENT_COLUMNS_START__*/", "/*__SENTIMENT_COLUMNS_END__*/")
OPTIONAL_NETWORK_MARKERS = (
    "/*__OPTIONAL_NETWORK_START__*/",
    "/*__OPTIONAL_NETWORK_END__*/",
)
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
PACKAGE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")


class MetadataError(ValueError):
    pass


@dataclass
class ValidationReport:
    kind: str
    source: str
    records: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    db_checks_run: bool = False

    @property
    def valid(self):
        return not self.errors

    def safe_dict(self):
        return {
            "kind": self.kind,
            "source": self.source,
            "valid": self.valid,
            "record_count": len(self.records),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "db_checks_run": self.db_checks_run,
            "db_check_status": "RUN" if self.db_checks_run else "NOT_RUN",
        }


def _load(path: str | Path | None, kind: str):
    if path is None:
        return None, f"{kind}:not-supplied"
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise MetadataError(f"{kind} manifest must be a regular non-symlink file")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError(f"{kind} manifest is not readable valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise MetadataError(f"{kind} manifest schema_version must be 1")
    return payload, p.name


def _uuid(value, label, errors, required=True):
    if value in (None, "") and not required:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        errors.append(f"{label}: invalid UUID")
        return None


def _utc(value, label, errors):
    try:
        text = str(value)
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
            raise ValueError
        return parsed.astimezone(dt.UTC)
    except (ValueError, TypeError):
        errors.append(f"{label}: timestamp must be ISO-8601 UTC")
        return None


def _date(value, label, errors, required=False):
    if value in (None, "") and not required:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        errors.append(f"{label}: invalid ISO date")
        return None


def _boolean(value, label, errors):
    if type(value) is not bool:
        errors.append(f"{label}: must be JSON true or false")
        return False
    return value


def _reject_template_controls(raw, prefix, errors):
    forbidden = (
        "\x00",
        "{{",
        "}}",
        "[[",
        "]]",
        *EXP_MARKERS,
        *REL_MARKERS,
        *CAP_MARKERS,
        *SENT_MARKERS,
        *OPTIONAL_NETWORK_MARKERS,
    )
    for name, value in raw.items():
        if isinstance(value, str) and any(token in value for token in forbidden):
            errors.append(f"{prefix}.{name}: contains reserved SQL/template control text")


def _opaque_id(prefix, *values):
    digest = hashlib.sha256("\x1f".join(str(v) for v in values).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def validate_experiment(path=None, db_records=None):
    payload, source = _load(path, "experiment")
    report = ValidationReport("experiment", source)
    if payload is None:
        report.warnings.append(
            "experiment manifest not supplied; typed empty relation will be used"
        )
        return report
    captures = payload.get("captures")
    devices = payload.get("allowed_device_models")
    profiles = payload.get("allowed_network_profiles")
    comparison_cohorts = payload.get("allowed_comparison_cohorts")
    if not isinstance(captures, list):
        report.errors.append("captures must be an array")
        return report
    if (
        not isinstance(devices, list)
        or not devices
        or not all(isinstance(x, str) and x for x in devices)
    ):
        report.errors.append("allowed_device_models must be a non-empty string array")
        devices = []
    if (
        not isinstance(profiles, list)
        or not profiles
        or not all(isinstance(x, str) and x for x in profiles)
    ):
        report.errors.append("allowed_network_profiles must be a non-empty string array")
        profiles = []
    allowed_cohorts = set()
    if not isinstance(comparison_cohorts, list) or not comparison_cohorts:
        report.errors.append(
            "allowed_comparison_cohorts must be a non-empty array of two-package arrays"
        )
    else:
        for index, cohort in enumerate(comparison_cohorts, 1):
            if (
                not isinstance(cohort, list)
                or len(cohort) != 2
                or len(set(cohort)) != 2
                or not all(isinstance(x, str) and PACKAGE.fullmatch(x) for x in cohort)
            ):
                report.errors.append(
                    f"allowed_comparison_cohorts[{index}]: expected two distinct package identifiers"
                )
                continue
            key = tuple(sorted(cohort))
            if key in allowed_cohorts:
                report.errors.append(f"allowed_comparison_cohorts[{index}]: duplicate cohort")
            allowed_cohorts.add(key)
    seen_capture = set()
    seen_trial = set()
    pairs = {}
    normalized = []
    for index, raw in enumerate(captures, 1):
        prefix = f"capture[{index}]"
        if not isinstance(raw, dict):
            report.errors.append(f"{prefix}: must be an object")
            continue
        missing = [x for x in EXPERIMENT_FIELDS if x not in raw]
        if missing:
            report.errors.append(f"{prefix}: missing fields {','.join(missing)}")
            continue
        row = dict(raw)
        row["comparison_cohort_id"] = None
        _reject_template_controls(raw, prefix, report.errors)
        for name in ("capture_id", "experiment_id", "session_id"):
            row[name] = _uuid(raw[name], f"{prefix}.{name}", report.errors)
        row["pair_id"] = _uuid(raw["pair_id"], f"{prefix}.pair_id", report.errors, False)
        try:
            row["trial_number"] = int(raw["trial_number"])
            if row["trial_number"] < 1:
                raise ValueError
        except (ValueError, TypeError):
            report.errors.append(f"{prefix}.trial_number: must be a positive integer")
            row["trial_number"] = None
        try:
            row["test_file_size_bytes"] = int(raw["test_file_size_bytes"])
            if row["test_file_size_bytes"] < 1:
                raise ValueError
        except (ValueError, TypeError):
            report.errors.append(f"{prefix}.test_file_size_bytes: must be positive")
            row["test_file_size_bytes"] = None
        started = _utc(
            raw["capture_started_at_utc"], f"{prefix}.capture_started_at_utc", report.errors
        )
        finished = _utc(
            raw["capture_finished_at_utc"], f"{prefix}.capture_finished_at_utc", report.errors
        )
        if started and finished and finished <= started:
            report.errors.append(f"{prefix}: capture finish must be after start")
        row["capture_started_at_utc"] = started
        row["capture_finished_at_utc"] = finished
        for name in (
            "transfer_completed",
            "capture_isolation_confirmed",
            "cache_cleared_or_download_verified",
        ):
            row[name] = _boolean(raw[name], f"{prefix}.{name}", report.errors)
        if type(raw["transfer_completed"]) is bool and not raw["transfer_completed"]:
            report.errors.append(f"{prefix}.transfer_completed: incomplete transfers are excluded")
        if (
            type(raw["capture_isolation_confirmed"]) is bool
            and not raw["capture_isolation_confirmed"]
        ):
            report.errors.append(
                f"{prefix}.capture_isolation_confirmed: required provenance is unconfirmed"
            )
        if (
            type(raw["cache_cleared_or_download_verified"]) is bool
            and not raw["cache_cleared_or_download_verified"]
        ):
            report.errors.append(
                f"{prefix}.cache_cleared_or_download_verified: required transfer provenance is unconfirmed"
            )
        if raw["scenario"] not in ("upload", "download"):
            report.errors.append(
                f"{prefix}.scenario: unsupported action; expected upload or download"
            )
        if raw["experiment_phase"] not in ("baseline", "followup", "ordinary"):
            report.errors.append(f"{prefix}.experiment_phase: unsupported phase")
        if not PACKAGE.fullmatch(str(raw["application_package"])):
            report.errors.append(f"{prefix}.application_package: invalid package identifier")
        if raw["application_package"] == "ir.rightel.myrightel":
            report.errors.append(
                f"{prefix}.application_package: deliberate MyRightel fixture is excluded"
            )
        if not HEX64.fullmatch(str(raw["test_file_sha256"])):
            report.errors.append(f"{prefix}.test_file_sha256: expected 64 hexadecimal characters")
        row["file_cohort_id"] = _opaque_id(
            "file", str(raw["test_file_sha256"]).lower(), row.get("test_file_size_bytes")
        )
        if raw["device_model"] not in devices:
            report.errors.append(f"{prefix}.device_model: not in allowed_device_models")
        if raw["network_profile"] not in profiles:
            report.errors.append(f"{prefix}.network_profile: not in allowed_network_profiles")
        for name in (
            "application_package",
            "scenario",
            "app_version",
            "device_model",
            "android_version",
            "network_type",
            "network_profile",
            "capture_tool",
            "capture_tool_version",
            "protocol_notes",
            "validation_notes",
        ):
            if not isinstance(raw[name], str) or not raw[name].strip():
                report.errors.append(f"{prefix}.{name}: required non-empty string")
        if row["capture_id"] in seen_capture:
            report.errors.append(f"{prefix}.capture_id: duplicate")
        seen_capture.add(row["capture_id"])
        trial_key = tuple(
            row.get(x)
            for x in (
                "experiment_id",
                "session_id",
                "application_package",
                "scenario",
                "file_cohort_id",
                "device_model",
                "android_version",
                "network_type",
                "network_profile",
                "app_version",
                "trial_number",
            )
        )
        if trial_key in seen_trial:
            report.errors.append(f"{prefix}: duplicate trial within the exact comparison condition")
        seen_trial.add(trial_key)
        if row["pair_id"]:
            pairs.setdefault(row["pair_id"], []).append((prefix, row))
        normalized.append(row)
    for pair_id, members in pairs.items():
        if len(members) != 2:
            report.errors.append(f"pair {pair_id}: must contain exactly two captures")
            continue
        a, b = members[0][1], members[1][1]
        equal_fields = (
            "experiment_id",
            "trial_number",
            "scenario",
            "test_file_sha256",
            "test_file_size_bytes",
            "device_model",
            "android_version",
            "network_type",
            "network_profile",
            "session_id",
            "capture_tool",
            "capture_tool_version",
            "experiment_phase",
        )
        if any(a.get(x) != b.get(x) for x in equal_fields):
            report.errors.append(f"pair {pair_id}: comparison conditions differ")
        cohort = tuple(sorted((a.get("application_package"), b.get("application_package"))))
        if a.get("application_package") == b.get("application_package"):
            report.errors.append(f"pair {pair_id}: paired applications must differ")
        elif cohort not in allowed_cohorts:
            report.errors.append(
                f"pair {pair_id}: applications are not an allowed comparison cohort"
            )
        else:
            cohort_id = _opaque_id("cohort", *cohort)
            a["comparison_cohort_id"] = cohort_id
            b["comparison_cohort_id"] = cohort_id
    if db_records is not None:
        report.db_checks_run = True
        by_id = {str(x.get("capture_id")): x for x in db_records}
        for i, row in enumerate(normalized, 1):
            db = by_id.get(row.get("capture_id"))
            if not db:
                report.errors.append(f"capture[{i}]: capture_id absent from database")
                continue
            if db.get("package_name") != row.get("application_package"):
                report.errors.append(f"capture[{i}]: database application attribution mismatch")
            if db.get("scenario") != row.get("scenario"):
                report.errors.append(f"capture[{i}]: database scenario mismatch")
            if int(db.get("transfer_file_size_bytes") or -1) != row.get("test_file_size_bytes"):
                report.errors.append(f"capture[{i}]: capture/manifest transfer size mismatch")
            if db.get("status") != "analyzed" or not row.get("transfer_completed"):
                report.errors.append(f"capture[{i}]: transfer or analysis is incomplete")
    else:
        report.warnings.append("database-aware capture checks were NOT run")
    report.records = normalized
    return report


def validate_release(path=None):
    payload, source = _load(path, "release")
    report = ValidationReport("release", source)
    if payload is None:
        report.warnings.append("release manifest not supplied; typed empty relation will be used")
        return report
    events = payload.get("release_events")
    if not isinstance(events, list):
        report.errors.append("release_events must be an array")
        return report
    seen = set()
    normalized = []
    for index, raw in enumerate(events, 1):
        prefix = f"release_event[{index}]"
        if not isinstance(raw, dict):
            report.errors.append(f"{prefix}: must be an object")
            continue
        missing = [x for x in RELEASE_FIELDS if x not in raw]
        if missing:
            report.errors.append(f"{prefix}: missing fields {','.join(missing)}")
            continue
        row = dict(raw)
        _reject_template_controls(raw, prefix, report.errors)
        row["release_event_id"] = _uuid(
            raw["release_event_id"], f"{prefix}.release_event_id", report.errors
        )
        if row["release_event_id"] in seen:
            report.errors.append(f"{prefix}.release_event_id: duplicate")
        seen.add(row["release_event_id"])
        for name in ("experiment_id_before", "experiment_id_after"):
            row[name] = _uuid(raw[name], f"{prefix}.{name}", report.errors, False)
        release = _date(raw["release_date"], f"{prefix}.release_date", report.errors, True)
        row["release_date"] = release
        row["release_date_verified"] = _boolean(
            raw["release_date_verified"], f"{prefix}.release_date_verified", report.errors
        )
        if raw["release_date_precision"] not in ("day", "month", "unknown"):
            report.errors.append(
                f"{prefix}.release_date_precision: expected day, month, or unknown"
            )
        if not PACKAGE.fullmatch(str(raw["application_package"])):
            report.errors.append(f"{prefix}.application_package: invalid package identifier")
        if raw["application_package"] == "ir.rightel.myrightel":
            report.errors.append(
                f"{prefix}.application_package: deliberate MyRightel fixture is excluded"
            )
        for name in (
            "previous_version",
            "new_version",
            "release_date_source",
            "release_evidence_reference",
        ):
            if not isinstance(raw[name], str) or not raw[name].strip():
                report.errors.append(f"{prefix}.{name}: required non-empty string")
        if raw["previous_version"] == raw["new_version"]:
            report.errors.append(f"{prefix}: previous_version and new_version must differ")
        dates = {}
        for name in (
            "baseline_start",
            "baseline_end",
            "followup_start",
            "followup_end",
            "transition_period_start",
            "transition_period_end",
        ):
            dates[name] = _date(raw[name], f"{prefix}.{name}", report.errors)
        if release and row["release_date_verified"] and raw["release_date_precision"] == "day":
            dates["baseline_start"] = dates["baseline_start"] or release - dt.timedelta(days=14)
            dates["baseline_end"] = dates["baseline_end"] or release - dt.timedelta(days=1)
            dates["followup_start"] = dates["followup_start"] or release + dt.timedelta(days=1)
            dates["followup_end"] = dates["followup_end"] or release + dt.timedelta(days=14)
            dates["transition_period_start"] = dates["transition_period_start"] or release
            dates["transition_period_end"] = dates["transition_period_end"] or release
        row.update(dates)
        if (
            dates["baseline_start"]
            and dates["baseline_end"]
            and dates["baseline_start"] > dates["baseline_end"]
        ):
            report.errors.append(f"{prefix}: baseline window is reversed")
        if (
            dates["followup_start"]
            and dates["followup_end"]
            and dates["followup_start"] > dates["followup_end"]
        ):
            report.errors.append(f"{prefix}: followup window is reversed")
        if release and dates["baseline_end"] and dates["baseline_end"] >= release:
            report.errors.append(f"{prefix}: baseline must end before release date")
        if release and dates["followup_start"] and dates["followup_start"] <= release:
            report.errors.append(f"{prefix}: followup must start after release date")
        if (
            dates["transition_period_start"]
            and dates["transition_period_end"]
            and (dates["transition_period_start"] > dates["transition_period_end"])
        ):
            report.errors.append(f"{prefix}: transition period is reversed")
        if (
            dates["baseline_end"]
            and dates["transition_period_start"]
            and (dates["baseline_end"] >= dates["transition_period_start"])
        ):
            report.errors.append(f"{prefix}: baseline overlaps transition period")
        if (
            dates["followup_start"]
            and dates["transition_period_end"]
            and (dates["followup_start"] <= dates["transition_period_end"])
        ):
            report.errors.append(f"{prefix}: followup overlaps transition period")
        normalized.append(row)
    for i, left in enumerate(normalized):
        for right in normalized[i + 1 :]:
            if left.get("application_package") != right.get("application_package"):
                continue
            left_start, left_end = left.get("baseline_start"), left.get("followup_end")
            right_start, right_end = right.get("baseline_start"), right.get("followup_end")
            if (
                left_start
                and left_end
                and right_start
                and right_end
                and (left_start <= right_end and right_start <= left_end)
            ):
                report.errors.append(
                    "release events for one application have overlapping analysis windows"
                )
    report.records = normalized
    return report


def _literal(value, cast):
    if value is None:
        return "NULL::" + cast
    if cast in ("bigint", "integer"):
        return str(int(value)) + "::" + cast
    if cast == "boolean":
        return ("true" if value else "false") + "::boolean"
    if isinstance(value, (dt.datetime, dt.date)):
        value = value.isoformat().replace("+00:00", "Z")
    return "'" + str(value).replace("'", "''") + "'::" + cast


EXP_TYPES = {
    **{x: "text" for x in EXPERIMENT_REPORT_FIELDS},
    "capture_id": "uuid",
    "experiment_id": "uuid",
    "session_id": "uuid",
    "pair_id": "uuid",
    "trial_number": "integer",
    "capture_started_at_utc": "timestamptz",
    "capture_finished_at_utc": "timestamptz",
    "transfer_completed": "boolean",
    "test_file_size_bytes": "bigint",
    "capture_isolation_confirmed": "boolean",
    "cache_cleared_or_download_verified": "boolean",
}
REL_TYPES = {
    **{x: "text" for x in RELEASE_REPORT_FIELDS},
    "release_event_id": "uuid",
    "release_date": "date",
    "release_date_verified": "boolean",
    "baseline_start": "date",
    "baseline_end": "date",
    "followup_start": "date",
    "followup_end": "date",
    "transition_period_start": "date",
    "transition_period_end": "date",
    "experiment_id_before": "uuid",
    "experiment_id_after": "uuid",
}


def typed_select(records, fields, types):
    if not records:
        return "SELECT " + ", ".join(f"NULL::{types[n]} AS {n}" for n in fields) + " WHERE false"
    rows = []
    for row in records:
        rows.append(
            "SELECT " + ", ".join(_literal(row.get(n), types[n]) + " AS " + n for n in fields)
        )
    return "\nUNION ALL\n".join(rows)


def capability_select(capabilities):
    fields = (
        "network_state",
        "experiment_state",
        "release_state",
        "sentiment_state",
        "topic_state",
    )
    row = {x: str(capabilities.get(x, "NOT_CHECKED")) for x in fields}
    return typed_select([row], fields, {x: "text" for x in fields})


def _replace(text, markers, replacement):
    start, end = markers
    if (start in text) != (end in text):
        raise MetadataError("unbalanced SQL compilation markers")
    if start not in text:
        return text
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise MetadataError("SQL compilation markers must be unique and ordered")
    return (
        text[: text.index(start) + len(start)] + "\n" + replacement + "\n" + text[text.index(end) :]
    )


def _optional_block(text, markers, enabled, disabled_replacement):
    start, end = markers
    if (start in text) != (end in text):
        raise MetadataError("unbalanced SQL compilation markers")
    if start not in text:
        return text
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise MetadataError("SQL compilation markers must be unique and ordered")
    before = text[: text.index(start)]
    body = text[text.index(start) + len(start) : text.index(end)]
    after = text[text.index(end) + len(end) :]
    return before + (body if enabled else "\n" + disabled_replacement + "\n") + after


def render_sql(text, experiment, release, capabilities):
    if not experiment.valid or not release.valid:
        raise MetadataError("invalid private metadata cannot be compiled")
    rendered = _replace(
        text,
        EXP_MARKERS,
        typed_select(experiment.records, EXPERIMENT_REPORT_FIELDS, EXP_TYPES),
    )
    rendered = _replace(
        rendered,
        REL_MARKERS,
        typed_select(release.records, RELEASE_REPORT_FIELDS, REL_TYPES),
    )
    rendered = _replace(rendered, CAP_MARKERS, capability_select(capabilities))
    network_available = capabilities.get("network_state") in {
        "NETWORK_SCHEMA_READY",
        "NETWORK_EMPTY",
        "NETWORK_DATA_AVAILABLE",
        "NETWORK_COMPARISON_INSUFFICIENT",
        "NETWORK_COMPARISON_READY",
    }
    empty_network = """network AS (
 SELECT NULL::uuid release_event_id,NULL::text application_package,NULL::text scenario,
  NULL::text file_cohort_id,NULL::bigint test_file_size_bytes,NULL::text device_model,
  NULL::text android_version,NULL::text network_type,NULL::text network_profile,
  NULL::text capture_tool,NULL::text capture_tool_version,
  NULL::bigint network_before_manifest_n,NULL::bigint network_after_manifest_n,
  NULL::bigint network_before_observed_n,NULL::bigint network_after_observed_n,
  NULL::bigint network_before_analyzed_n,NULL::bigint network_after_analyzed_n,
  NULL::bigint before_throughput_eligible_n,NULL::bigint after_throughput_eligible_n,
  NULL::bigint before_amplification_eligible_n,NULL::bigint after_amplification_eligible_n,
  NULL::bigint before_recovery_eligible_n,NULL::bigint after_recovery_eligible_n,
  NULL::timestamptz network_latest_source_at WHERE false
)"""
    rendered = _optional_block(
        rendered, OPTIONAL_NETWORK_MARKERS, network_available, empty_network
    )
    sentiment_columns = (
        "ro.sentiment_status::text AS sentiment_status, "
        "ro.sentiment_label::text AS sentiment_label, "
        "true::boolean AS sentiment_capability_available"
        if capabilities.get("sentiment_state") == "SENTIMENT_DATA_AVAILABLE"
        else "NULL::text AS sentiment_status, NULL::text AS sentiment_label, "
        "false::boolean AS sentiment_capability_available"
    )
    return _replace(rendered, SENT_MARKERS, sentiment_columns)


def manifest_fingerprint(report):
    safe = [
        {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in r.items()}
        for r in report.records
    ]
    return hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
