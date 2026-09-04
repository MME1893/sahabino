from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sahabino.app_registry.schemas import (
    ApplicationCreate,
    ApplicationRead,
    ApplicationUpdate,
    CategoryRead,
)
from sahabino.app_registry.service import ApplicationRegistryService
from sahabino.db.session import get_session

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/applications",
    response_model=ApplicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    payload: ApplicationCreate,
    session: SessionDependency,
) -> ApplicationRead:
    application = await ApplicationRegistryService(session).create_application(payload)
    return ApplicationRead.from_application(application)


@router.get("/applications", response_model=list[ApplicationRead])
async def list_applications(
    session: SessionDependency,
    active: Annotated[bool | None, Query()] = None,
) -> list[ApplicationRead]:
    applications = await ApplicationRegistryService(session).list_applications(active)
    return [ApplicationRead.from_application(application) for application in applications]


@router.get("/applications/{application_id}", response_model=ApplicationRead)
async def get_application(
    application_id: UUID,
    session: SessionDependency,
) -> ApplicationRead:
    application = await ApplicationRegistryService(session).get_application(application_id)
    return ApplicationRead.from_application(application)


@router.patch("/applications/{application_id}", response_model=ApplicationRead)
async def update_application(
    application_id: UUID,
    payload: ApplicationUpdate,
    session: SessionDependency,
) -> ApplicationRead:
    application = await ApplicationRegistryService(session).update_application(
        application_id,
        payload,
    )
    return ApplicationRead.from_application(application)


# for now, just SOFT DELETING, maybe changing this.
@router.delete(
    "/applications/{application_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def deactivate_application(
    application_id: UUID,
    session: SessionDependency,
) -> Response:
    await ApplicationRegistryService(session).deactivate_application(application_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(session: SessionDependency) -> list[CategoryRead]:
    categories = await ApplicationRegistryService(session).list_categories()
    return [CategoryRead.from_category(category) for category in categories]
