from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

SEEDED_CATEGORY_CODES = [
    "chat_dating",
    "messaging",
    "operator",
    "social_network",
    "video",
    "word_game",
]


def test_categories_are_seeded_and_read_only(api_client: TestClient) -> None:
    response = api_client.get("/categories")

    assert response.status_code == 200
    categories = response.json()
    assert [category["code"] for category in categories] == SEEDED_CATEGORY_CODES
    assert all(category["name"].strip() for category in categories)

    category_operations = api_client.get("/openapi.json").json()["paths"]["/categories"]
    assert "get" in category_operations
    assert {"post", "patch", "delete"}.isdisjoint(category_operations)


def test_create_application_persists_one_primary_category(
    api_client: TestClient,
    db_connection: psycopg.Connection[Any],
) -> None:
    payload = {
        "name": "Telegram",
        "package_name": "org.telegram.messenger",
        "category_codes": ["messaging", "social_network"],
        "primary_category_code": "messaging",
    }

    response = api_client.post("/applications", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == payload["name"]
    assert body["package_name"] == payload["package_name"]
    assert body["is_active"] is True
    assert body["deactivated_at"] is None
    assert body["created_at"]
    assert body["updated_at"]
    assert {category["code"]: category["is_primary"] for category in body["categories"]} == {
        "messaging": True,
        "social_network": False,
    }

    application = db_connection.execute(
        """
        SELECT id, name, package_name, is_active, deactivated_at
        FROM applications
        WHERE id = %s
        """,
        (body["id"],),
    ).fetchone()
    assert application is not None
    assert str(application[0]) == body["id"]
    assert application[1:] == (
        "Telegram",
        "org.telegram.messenger",
        True,
        None,
    )

    assignments = db_connection.execute(
        """
        SELECT categories.code, application_categories.is_primary
        FROM application_categories
        JOIN categories ON categories.id = application_categories.category_id
        WHERE application_categories.application_id = %s
        ORDER BY categories.code
        """,
        (body["id"],),
    ).fetchall()
    assert assignments == [("messaging", True), ("social_network", False)]
    assert sum(is_primary for _, is_primary in assignments) == 1


def test_duplicate_package_name_returns_conflict(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
) -> None:
    create_application(package_name="com.example.duplicate")

    response = api_client.post(
        "/applications",
        json={
            "name": "Duplicate",
            "package_name": "com.example.duplicate",
            "category_codes": ["video"],
            "primary_category_code": "video",
        },
    )

    assert response.status_code == 409
    assert "package" in response.text.lower()


@pytest.mark.parametrize(
    "category_codes,primary_category_code",
    [
        (["unknown"], "unknown"),
        (["messaging"], "video"),
    ],
)
def test_create_rejects_invalid_category_assignments(
    api_client: TestClient,
    db_connection: psycopg.Connection[Any],
    category_codes: list[str],
    primary_category_code: str,
) -> None:
    response = api_client.post(
        "/applications",
        json={
            "name": "Invalid categories",
            "package_name": "com.example.invalid",
            "category_codes": category_codes,
            "primary_category_code": primary_category_code,
        },
    )

    assert response.status_code == 422
    count = db_connection.execute(
        "SELECT count(*) FROM applications WHERE package_name = %s",
        ("com.example.invalid",),
    ).fetchone()
    assert count == (0,)


def test_get_application_returns_its_categories(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
) -> None:
    created = create_application()

    response = api_client.get(f"/applications/{created['id']}")

    assert response.status_code == 200
    retrieved = response.json()
    assert {
        field: retrieved[field]
        for field in (
            "id",
            "name",
            "package_name",
            "is_active",
            "deactivated_at",
            "created_at",
            "updated_at",
        )
    } == {
        field: created[field]
        for field in (
            "id",
            "name",
            "package_name",
            "is_active",
            "deactivated_at",
            "created_at",
            "updated_at",
        )
    }
    assert {category["code"]: category["is_primary"] for category in retrieved["categories"]} == {
        category["code"]: category["is_primary"] for category in created["categories"]
    }


def test_list_applications_supports_active_filter(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
) -> None:
    active = create_application(name="Active", package_name="com.example.active")
    inactive = create_application(name="Inactive", package_name="com.example.inactive")
    assert api_client.delete(f"/applications/{inactive['id']}").status_code == 204

    all_applications = api_client.get("/applications")
    active_applications = api_client.get("/applications", params={"active": "true"})
    inactive_applications = api_client.get("/applications", params={"active": "false"})

    assert all_applications.status_code == 200
    all_ids = [application["id"] for application in all_applications.json()]
    assert set(all_ids) == {active["id"], inactive["id"]}
    assert api_client.get("/applications").json() == all_applications.json()
    assert [application["id"] for application in active_applications.json()] == [active["id"]]
    assert [application["id"] for application in inactive_applications.json()] == [inactive["id"]]


def test_patch_updates_fields_and_replaces_categories_atomically(
    api_client: TestClient,
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    created = create_application()

    field_response = api_client.patch(
        f"/applications/{created['id']}",
        json={"name": "Renamed", "package_name": "com.example.renamed"},
    )
    assert field_response.status_code == 200
    assert field_response.json()["name"] == "Renamed"
    assert field_response.json()["package_name"] == "com.example.renamed"
    assert field_response.json()["updated_at"] != created["updated_at"]

    category_response = api_client.patch(
        f"/applications/{created['id']}",
        json={
            "category_codes": ["video", "word_game"],
            "primary_category_code": "word_game",
        },
    )
    assert category_response.status_code == 200
    assert {
        category["code"]: category["is_primary"]
        for category in category_response.json()["categories"]
    } == {"video": False, "word_game": True}

    persisted_assignments = db_connection.execute(
        """
        SELECT categories.code, application_categories.is_primary
        FROM application_categories
        JOIN categories ON categories.id = application_categories.category_id
        WHERE application_categories.application_id = %s
        ORDER BY categories.code
        """,
        (created["id"],),
    ).fetchall()
    assert persisted_assignments == [("video", False), ("word_game", True)]
    assert sum(is_primary for _, is_primary in persisted_assignments) == 1

    invalid_response = api_client.patch(
        f"/applications/{created['id']}",
        json={
            "name": "Must not be persisted",
            "category_codes": ["video", "unknown"],
            "primary_category_code": "video",
        },
    )
    assert invalid_response.status_code == 422
    after_invalid_update = api_client.get(f"/applications/{created['id']}").json()
    assert after_invalid_update["name"] == "Renamed"
    assert after_invalid_update["categories"] == category_response.json()["categories"]


@pytest.mark.parametrize(
    "payload",
    [
        {"category_codes": ["messaging"]},
        {"primary_category_code": "messaging"},
    ],
)
def test_patch_requires_category_codes_and_primary_together(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    created = create_application()

    response = api_client.patch(f"/applications/{created['id']}", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field",
    ["is_active", "created_at", "updated_at", "deactivated_at", "unexpected"],
)
def test_patch_rejects_internal_and_unknown_fields(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
    field: str,
) -> None:
    created = create_application()

    response = api_client.patch(
        f"/applications/{created['id']}",
        json={field: "value"},
    )

    assert response.status_code == 422


def test_patch_duplicate_package_name_returns_conflict_without_changing_row(
    api_client: TestClient,
    create_application: Callable[..., dict[str, Any]],
) -> None:
    first = create_application(package_name="com.example.first")
    second = create_application(package_name="com.example.second")

    response = api_client.patch(
        f"/applications/{second['id']}",
        json={"name": "Must not change", "package_name": first["package_name"]},
    )

    assert response.status_code == 409
    persisted = api_client.get(f"/applications/{second['id']}").json()
    assert persisted["name"] == second["name"]
    assert persisted["package_name"] == second["package_name"]


def test_delete_is_idempotent_and_keeps_application_and_assignments(
    api_client: TestClient,
    db_connection: psycopg.Connection[Any],
    create_application: Callable[..., dict[str, Any]],
) -> None:
    created = create_application()

    first_response = api_client.delete(f"/applications/{created['id']}")
    first_deactivation = api_client.get(f"/applications/{created['id']}").json()
    second_response = api_client.delete(f"/applications/{created['id']}")
    second_deactivation = api_client.get(f"/applications/{created['id']}").json()

    assert first_response.status_code == 204
    assert first_response.content == b""
    assert second_response.status_code == 204
    assert second_deactivation["deactivated_at"] == first_deactivation["deactivated_at"]

    stored = db_connection.execute(
        """
        SELECT is_active, deactivated_at,
               (SELECT count(*) FROM application_categories
                WHERE application_id = applications.id)
        FROM applications
        WHERE id = %s
        """,
        (created["id"],),
    ).fetchone()
    assert stored is not None
    assert stored[0] is False
    assert stored[1] is not None
    assert stored[1].tzinfo is not None
    assert stored[2] == 2


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_application_not_found_returns_404(
    api_client: TestClient,
    method: str,
) -> None:
    application_id = uuid4()
    request = getattr(api_client, method)
    kwargs = {"json": {"name": "Missing"}} if method == "patch" else {}

    response = request(f"/applications/{application_id}", **kwargs)

    assert response.status_code == 404
