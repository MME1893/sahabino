from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sahabino.app_registry.models import Application, Category

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _validate_category_assignment(category_codes: list[str], primary_category_code: str) -> None:
    if len(category_codes) != len(set(category_codes)):
        raise ValueError("category_codes must not contain duplicates")
    if primary_category_code not in category_codes:
        raise ValueError("primary_category_code must be included in category_codes")


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    package_name: NonEmptyString
    category_codes: list[NonEmptyString] = Field(min_length=1)
    primary_category_code: NonEmptyString

    @model_validator(mode="after")
    def validate_category_assignment(self) -> Self:
        _validate_category_assignment(
            self.category_codes,
            self.primary_category_code,
        )
        return self


class ApplicationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString | None = None
    package_name: NonEmptyString | None = None
    category_codes: list[NonEmptyString] | None = Field(default=None, min_length=1)
    primary_category_code: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        field_order = ("name", "package_name", "category_codes", "primary_category_code")
        for field_name in field_order:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")

        category_fields = {"category_codes", "primary_category_code"}
        supplied_category_fields = category_fields & self.model_fields_set
        if supplied_category_fields and supplied_category_fields != category_fields:
            raise ValueError("category_codes and primary_category_code must be provided together")

        if self.category_codes is not None and self.primary_category_code is not None:
            _validate_category_assignment(
                self.category_codes,
                self.primary_category_code,
            )
        return self


class CategoryRead(BaseModel):
    code: str
    name: str

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_category(cls, category: Category) -> Self:
        return cls.model_validate(category)


class ApplicationCategoryRead(BaseModel):
    code: str
    name: str
    is_primary: bool


class ApplicationRead(BaseModel):
    id: UUID
    name: str
    package_name: str
    is_active: bool
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    categories: list[ApplicationCategoryRead]

    @classmethod
    def from_application(cls, application: Application) -> Self:
        # numbers of categories for an application is small so we are ok here to use python sorting
        assignments = sorted(
            application.category_assignments,
            key=lambda assignment: assignment.category.code,
        )
        return cls(
            id=application.id,
            name=application.name,
            package_name=application.package_name,
            is_active=application.is_active,
            deactivated_at=application.deactivated_at,
            created_at=application.created_at,
            updated_at=application.updated_at,
            categories=[
                ApplicationCategoryRead(
                    code=assignment.category.code,
                    name=assignment.category.name,
                    is_primary=assignment.is_primary,
                )
                for assignment in assignments
            ],
        )
