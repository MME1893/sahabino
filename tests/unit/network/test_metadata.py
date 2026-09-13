from __future__ import annotations

import importlib

from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from sahabino.network.models import (
    NetworkAnalysisResult,
    NetworkCapture,
    NetworkCaptureIdempotencyKey,
)


def test_tcp_receiver_stall_constraint_name_matches_migration() -> None:
    migration = importlib.import_module(
        "migrations.versions.20260912_0004_network_capture_analysis"
    )
    migration_names = {
        f"ck_network_analysis_results_{constraint.name}"
        for constraint in migration._range_constraints()
    }
    metadata_names = {
        constraint.name
        for constraint in NetworkAnalysisResult.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    expected = "ck_network_analysis_results_tcp_receiver_stall_ratio_range"
    assert expected in migration_names
    assert expected in metadata_names
    assert "ck_network_analysis_results_tcp_stall_ratio_range" not in metadata_names


def test_idempotency_mapping_metadata_matches_unmerged_migration() -> None:
    migration = importlib.import_module(
        "migrations.versions.20260912_0004_network_capture_analysis"
    )
    migration_capture_columns = {column.name for column in migration._capture_columns()}
    migration_mapping_columns = {column.name for column in migration._idempotency_key_columns()}

    assert "idempotency_key" not in migration_capture_columns
    assert "idempotency_key" not in NetworkCapture.__table__.columns
    assert migration_mapping_columns == set(NetworkCaptureIdempotencyKey.__table__.columns.keys())
    assert NetworkCaptureIdempotencyKey.__table__.primary_key.name == (
        "pk_network_capture_idempotency_keys"
    )
    foreign_key = next(
        constraint
        for constraint in NetworkCaptureIdempotencyKey.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert foreign_key.name == ("fk_network_capture_idempotency_keys_capture_id_network_captures")
    assert foreign_key.ondelete == "RESTRICT"
