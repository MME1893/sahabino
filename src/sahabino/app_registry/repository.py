from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sahabino.app_registry.models import (
    Application,
    ApplicationCategory,
    Category,
)


# we have define two layer app_repo and app_service to get data from DB
# wanna let app_repo handle DBMS stuff and app_sevice handle bussiness stuff
class ApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_categories(self) -> list[Category]:
        result = await self._session.scalars(select(Category).order_by(Category.code))
        return list(result)

    async def get_categories_by_codes(self, codes: Sequence[str]) -> list[Category]:
        result = await self._session.scalars(
            select(Category).where(Category.code.in_(codes)).order_by(Category.code)
        )
        return list(result)

    async def get_application(self, application_id: UUID) -> Application | None:
        return await self._session.scalar(
            select(Application)
            .where(Application.id == application_id)
            .options(
                selectinload(Application.category_assignments).joinedload(
                    ApplicationCategory.category
                )
            )
        )

    async def list_applications(self, active: bool | None) -> list[Application]:
        statement = select(Application).options(
            selectinload(Application.category_assignments).joinedload(ApplicationCategory.category)
        )
        if active is not None:
            statement = statement.where(Application.is_active.is_(active))
        statement = statement.order_by(Application.created_at, Application.id)
        result = await self._session.scalars(statement)
        return list(result)

    def add_application(self, application: Application) -> None:
        self._session.add(application)

    def set_category_assignments(
        self,
        application: Application,
        categories: Sequence[Category],
        primary_category_code: str,
    ) -> None:
        application.category_assignments = [
            ApplicationCategory(
                category=category,
                is_primary=category.code == primary_category_code,
            )
            for category in sorted(categories, key=lambda item: item.code)
        ]

    async def replace_category_assignments(
        self,
        application: Application,
        categories: Sequence[Category],
        primary_category_code: str,
    ) -> None:
        # so deleting first avoids a transient clash with the partial unique index when
        #  a different category becomes primary but both flushes remain inside
        # the service transaction so other transactions never observe the gap and because
        # of that i have not using transactions here
        application.category_assignments.clear()
        await self._session.flush()
        self.set_category_assignments(
            application,
            categories,
            primary_category_code,
        )

    async def flush(self) -> None:
        await self._session.flush()
