from __future__ import annotations

import pytest
from pydantic import ValidationError

from sahabino.app_registry.schemas import ApplicationCreate, ApplicationUpdate

VALID_CREATE = {
    "name": "Telegram",
    "package_name": "org.telegram.messenger",
    "category_codes": ["messaging", "social_network"],
    "primary_category_code": "messaging",
}


@pytest.mark.parametrize("field", ["name", "package_name"])
@pytest.mark.parametrize("invalid_value", ["", "   "])
def test_create_rejects_blank_required_text(field: str, invalid_value: str) -> None:
    payload = {**VALID_CREATE, field: invalid_value}

    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate(payload)


def test_create_requires_at_least_one_category() -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate({**VALID_CREATE, "category_codes": []})


def test_create_rejects_duplicate_category_codes() -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate(
            {**VALID_CREATE, "category_codes": ["messaging", "messaging"]}
        )


def test_create_requires_primary_category_to_be_assigned() -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate({**VALID_CREATE, "primary_category_code": "video"})


def test_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate({**VALID_CREATE, "unexpected": "value"})


@pytest.mark.parametrize(
    "payload",
    [
        {"language_code": "fa"},
        {"country_code": "ir"},
        {"language_code": "fa", "country_code": None},
        {"language_code": None, "country_code": "ir"},
    ],
)
def test_create_requires_complete_locale_pair(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate({**VALID_CREATE, **payload})


@pytest.mark.parametrize(
    "payload",
    [
        {"language_code": "FA", "country_code": "ir"},
        {"language_code": "fa", "country_code": "IRN"},
        {"language_code": "", "country_code": "ir"},
    ],
)
def test_create_rejects_invalid_locale_codes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate.model_validate({**VALID_CREATE, **payload})


@pytest.mark.parametrize(
    "payload",
    [
        {"category_codes": ["messaging"]},
        {"primary_category_code": "messaging"},
    ],
)
def test_update_requires_complete_category_assignment(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate(payload)


def test_update_rejects_duplicate_category_codes() -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate(
            {
                "category_codes": ["messaging", "messaging"],
                "primary_category_code": "messaging",
            }
        )


def test_update_requires_primary_category_to_be_assigned() -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate(
            {
                "category_codes": ["messaging"],
                "primary_category_code": "video",
            }
        )


def test_update_allows_ordinary_partial_update() -> None:
    update = ApplicationUpdate.model_validate({"name": "Renamed"})

    assert update.model_dump(exclude_unset=True) == {"name": "Renamed"}


@pytest.mark.parametrize(
    "payload",
    [
        {"language_code": "fa"},
        {"country_code": "ir"},
        {"language_code": "fa", "country_code": None},
        {"language_code": None, "country_code": "ir"},
    ],
)
def test_update_requires_complete_locale_pair(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate(payload)


def test_update_allows_atomic_locale_update_and_clear() -> None:
    update = ApplicationUpdate.model_validate({"language_code": "fa", "country_code": "ir"})
    clear = ApplicationUpdate.model_validate({"language_code": None, "country_code": None})

    assert update.model_dump(exclude_unset=True) == {
        "language_code": "fa",
        "country_code": "ir",
    }
    assert clear.model_dump(exclude_unset=True) == {
        "language_code": None,
        "country_code": None,
    }


@pytest.mark.parametrize("field", ["name", "package_name"])
@pytest.mark.parametrize("invalid_value", ["", "   "])
def test_update_rejects_blank_supplied_text(field: str, invalid_value: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate({field: invalid_value})


@pytest.mark.parametrize(
    "field",
    ["name", "package_name", "category_codes", "primary_category_code"],
)
def test_update_rejects_explicit_null(field: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate({field: None})


@pytest.mark.parametrize(
    "field",
    ["is_active", "created_at", "updated_at", "deactivated_at", "unexpected"],
)
def test_update_rejects_internal_and_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate({field: "value"})
