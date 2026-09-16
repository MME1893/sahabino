from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest


def test_application_lifecycle_check_constraint(
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    application = create_application()

    with pytest.raises(psycopg.errors.CheckViolation):
        db_connection.execute(
            """
            UPDATE applications
            SET is_active = FALSE, deactivated_at = NULL
            WHERE id = %s
            """,
            (application["id"],),
        )
    db_connection.rollback()


def test_application_locale_pair_check_constraint(
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    application = create_application()

    with pytest.raises(psycopg.errors.CheckViolation):
        db_connection.execute(
            "UPDATE applications SET language_code = 'fa' WHERE id = %s",
            (application["id"],),
        )
    db_connection.rollback()


def test_only_one_primary_category_is_allowed_by_database(
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    application = create_application()

    with pytest.raises(psycopg.errors.UniqueViolation):
        db_connection.execute(
            """
            UPDATE application_categories
            SET is_primary = TRUE
            WHERE application_id = %s AND is_primary = FALSE
            """,
            (application["id"],),
        )
    db_connection.rollback()


def test_category_delete_is_restricted(
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    create_application()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db_connection.execute("DELETE FROM categories WHERE code = 'messaging'")
    db_connection.rollback()


def test_physical_application_delete_cascades_assignments(
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    application = create_application()

    db_connection.execute("DELETE FROM applications WHERE id = %s", (application["id"],))
    remaining = db_connection.execute(
        "SELECT count(*) FROM application_categories WHERE application_id = %s",
        (application["id"],),
    ).fetchone()

    assert remaining == (0,)
