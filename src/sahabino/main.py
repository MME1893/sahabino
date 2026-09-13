import logging
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
from sahabino.common.config import get_settings
from sahabino.common.observability import configure_logging
from sahabino.db.session import dispose_engine
from sahabino.network.dependencies import close_network_runtime
from sahabino.network.exceptions import (
    CaptureDispatchError,
    CaptureNotFoundError,
    CaptureTooLargeError,
    NetworkCaptureError,
    StorageUnavailableError,
)
from sahabino.network.router import router as network_router

settings = get_settings()
configure_logging(
    service_name="sahabino-api",
    level=settings.log_level,
    log_format=settings.log_format,
    environment=settings.environment,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("API process started", extra={"event": "api.started"})
    try:
        yield
    finally:
        try:
            close_network_runtime()
        finally:
            try:
                await dispose_engine()
            finally:
                logger.info("API process stopped", extra={"event": "api.stopped"})


def create_app() -> FastAPI:
    application = FastAPI(title="Sahabino", lifespan=lifespan)
    application.include_router(registry_router)
    application.include_router(network_router)

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

    @application.exception_handler(CaptureNotFoundError)
    async def capture_not_found_handler(
        _request: Request, error: CaptureNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": {"code": error.code, "message": str(error)}},
        )

    @application.exception_handler(CaptureTooLargeError)
    async def capture_too_large_handler(
        _request: Request, error: CaptureTooLargeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": {"code": error.code, "message": str(error)}},
        )

    @application.exception_handler(StorageUnavailableError)
    @application.exception_handler(CaptureDispatchError)
    async def network_dependency_handler(
        _request: Request, error: NetworkCaptureError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": {"code": error.code, "message": str(error)}},
        )

    @application.exception_handler(NetworkCaptureError)
    async def network_capture_handler(
        _request: Request, error: NetworkCaptureError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": {"code": error.code, "message": str(error)}},
        )

    return application


app = create_app()
