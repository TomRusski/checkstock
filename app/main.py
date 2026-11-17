"""Точка входа FastAPI: API + одна HTML-страница без сборки фронтенда."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError

from app.api import router as api_router
from app.config import get_settings
from app.db import SessionLocal
from app.errors import DomainError
from app.locations import ensure_singletons
from app.seed import seed_if_empty

logger = logging.getLogger("checkstock")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    with SessionLocal() as session:
        ensure_singletons(session)
        if settings.seed_on_start and seed_if_empty(session):
            logger.info("База была пустой — созданы тестовые данные из ТЗ")
        session.commit()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="CheckStock",
        description="Где сейчас товар: остатки по ФФ, резерв FBS, товар в пути, FBO.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(api_router)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        # Сюда попадают гонки за уникальные ключи (идемпотентность, имена складов).
        logger.warning("IntegrityError на %s: %s", request.url.path, exc.orig)
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "CONFLICT",
                    "message": "Конфликт данных: такой запрос уже выполняется или объект уже есть",
                    "details": {},
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Проверьте поля запроса",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {})

    return app


app = create_app()
