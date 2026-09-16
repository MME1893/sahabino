from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sahabino.app_registry.exceptions import (
    ApplicationNotFoundError,
    DuplicatePackageNameError,
    InvalidCategoryAssignmentError,
)
from sahabino.app_registry.models import Application, Category
from sahabino.app_registry.repository import ApplicationRepository
from sahabino.app_registry.schemas import ApplicationCreate, ApplicationUpdate

PACKAGE_NAME_CONSTRAINT = "uq_applications_package_name"


def validate_category_assignment(
    category_codes: list[str],
    primary_category_code: str,
) -> None:
    """Enforce invariants that must hold before assignments are persisted."""
    if not category_codes:
        raise InvalidCategoryAssignmentError("category_codes must contain at least one category")
    if len(category_codes) != len(set(category_codes)):
        raise InvalidCategoryAssignmentError("category_codes must not contain duplicates")
    if primary_category_code not in category_codes:
        raise InvalidCategoryAssignmentError(
            "primary_category_code must be included in category_codes"
        )


def _is_package_name_conflict(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == PACKAGE_NAME_CONSTRAINT


class ApplicationRegistryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = ApplicationRepository(session)

    async def list_categories(self) -> list[Category]:
        return await self._repository.list_categories()

    async def create_application(self, data: ApplicationCreate) -> Application:
        validate_category_assignment(
            data.category_codes,
            data.primary_category_code,
        )
        try:
            async with self._session.begin():
                categories = await self._resolve_categories(data.category_codes)
                application = Application(
                    name=data.name,
                    package_name=data.package_name,
                    language_code=data.language_code,
                    country_code=data.country_code,
                    category_assignments=[],
                )
                self._repository.set_category_assignments(
                    application,
                    categories,
                    data.primary_category_code,
                )
                self._repository.add_application(application)
                await self._repository.flush()
        except IntegrityError as error:
            if _is_package_name_conflict(error):
                raise DuplicatePackageNameError(data.package_name) from error
            raise
        return application

    async def get_application(self, application_id: UUID) -> Application:
        application = await self._repository.get_application(application_id)
        if application is None:
            raise ApplicationNotFoundError(application_id)
        return application

    async def list_applications(self, active: bool | None) -> list[Application]:
        return await self._repository.list_applications(active)

    async def update_application(
        self,
        application_id: UUID,
        data: ApplicationUpdate,
    ) -> Application:
        changes = data.model_dump(exclude_unset=True)
        try:
            async with self._session.begin():
                application = await self._repository.get_application(application_id)
                if application is None:
                    raise ApplicationNotFoundError(application_id)

                if "name" in changes:
                    assert data.name is not None
                    application.name = data.name
                if "package_name" in changes:
                    assert data.package_name is not None
                    application.package_name = data.package_name
                if "language_code" in changes:
                    application.language_code = data.language_code
                    application.country_code = data.country_code

                if "category_codes" in changes:
                    category_codes = data.category_codes
                    primary_category_code = data.primary_category_code
                    if category_codes is None or primary_category_code is None:
                        raise InvalidCategoryAssignmentError(
                            "category_codes and primary_category_code must be provided together"
                        )
                    validate_category_assignment(
                        category_codes,
                        primary_category_code,
                    )
                    categories = await self._resolve_categories(category_codes)
                    await self._repository.replace_category_assignments(
                        application,
                        categories,
                        primary_category_code,
                    )

                if changes:
                    application.updated_at = datetime.now(UTC)
                    await self._repository.flush()
        except IntegrityError as error:
            if _is_package_name_conflict(error):
                assert data.package_name is not None
                raise DuplicatePackageNameError(data.package_name) from error
            raise
        return application

    async def deactivate_application(self, application_id: UUID) -> None:
        async with self._session.begin():
            application = await self._repository.get_application(application_id)
            if application is None:
                raise ApplicationNotFoundError(application_id)
            if application.is_active:
                deactivated_at = datetime.now(UTC)
                application.is_active = False
                application.deactivated_at = deactivated_at
                application.updated_at = deactivated_at
                await self._repository.flush()

    async def _resolve_categories(self, category_codes: list[str]) -> list[Category]:
        categories = await self._repository.get_categories_by_codes(category_codes)
        found_codes = {category.code for category in categories}
        missing_codes = sorted(set(category_codes) - found_codes)
        if missing_codes:
            joined_codes = ", ".join(missing_codes)
            raise InvalidCategoryAssignmentError(f"Unknown category codes: {joined_codes}")
        return categories
