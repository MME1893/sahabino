from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from sahabino.app_registry.exceptions import (
    ApplicationNotFoundError,
    DuplicatePackageNameError,
    InvalidCategoryAssignmentError,
)
from sahabino.app_registry.router import router as registry_router
from sahabino.db.session import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    application = FastAPI(title="Sahabino", lifespan=lifespan)
    application.include_router(registry_router)

    @application.exception_handler(ApplicationNotFoundError)
    async def application_not_found_handler(
        _request: Request,
        error: ApplicationNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(error)},
        )

    @application.exception_handler(DuplicatePackageNameError)
    async def duplicate_package_name_handler(
        _request: Request,
        error: DuplicatePackageNameError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(error)},
        )

    @application.exception_handler(InvalidCategoryAssignmentError)
    async def invalid_category_assignment_handler(
        _request: Request,
        error: InvalidCategoryAssignmentError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(error)},
        )

    return application


app = create_app()
