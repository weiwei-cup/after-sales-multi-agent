"""FastAPI lifecycle and transport validation; workflow logic lives in services."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from after_sales.api.contracts import (
    Accepted,
    CreateTicket,
    EmptyRequest,
    ErrorView,
    EventPage,
    PendingResponse,
    Principal,
    RunView,
    StartRun,
    TicketView,
)
from after_sales.config import Settings
from after_sales.domain.models import Identifier
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationError, ApplicationService

# Public local demo credentials. Keep real deployment authentication outside this learning phase.
DEMO_IDENTITIES = {
    "demo-customer-a": Principal(role="customer", actor_id="CUST-A"),
    "demo-customer-b": Principal(role="customer", actor_id="CUST-B"),
    "demo-operator": Principal(role="operator", actor_id="OP-DEMO"),
}
security = HTTPBearer(auto_error=False)


def identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
    if credentials and credentials.credentials.isascii() and credentials.scheme.lower() == "bearer":
        for token, principal in DEMO_IDENTITIES.items():
            if secrets.compare_digest(credentials.credentials, token):
                return principal
    raise ApplicationError(401, "UNAUTHENTICATED", "请提供有效的本地演示身份令牌。")


Identity = Annotated[Principal, Depends(identity)]
RequestKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
]
PageLimit = Annotated[int, Query(ge=1, le=100)]


def create_app(settings=None, *, service=None):
    # Defer environment/config/database/model setup until lifespan, not module import.
    @asynccontextmanager
    async def lifespan(app):
        configured = settings or Settings()
        app.state.service = service or ApplicationService(
            BusinessRepository(configured.business_db_path),
            configured,
            workers=configured.api_workers,
            capacity=configured.api_queue_capacity,
        )
        await asyncio.to_thread(app.state.service.start)
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.service.close)

    errors = {code: {"model": ErrorView} for code in (401, 403, 404, 409, 422, 500, 503)}
    app = FastAPI(
        title="售后多 Agent 本地服务",
        version="0.8.0",
        lifespan=lifespan,
        responses=errors,
        description="P08：脚本模型、固定演示身份、单进程执行器。",
    )

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def failure(request, status, code, message):
        request_id = getattr(request.state, "request_id", uuid4().hex)
        return JSONResponse(
            status_code=status,
            content=ErrorView(code=code, message=message, request_id=request_id).model_dump(),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(ApplicationError)
    async def application_error(request, error):
        return failure(request, error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid(request, error):
        kind = error.errors()[0]["type"] if error.errors() else "invalid"
        message = {
            "missing": "请求缺少必需的字段或请求头。",
            "extra_forbidden": "请求包含不允许的字段。",
            "int_type": "金额和版本必须使用整数。",
        }.get(kind, "请求字段、格式或字段组合无效。")
        return failure(request, 422, "INVALID_REQUEST", message)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return failure(request, error.status_code, "HTTP_ERROR", "请求路径或方法无效。")

    @app.exception_handler(Exception)
    async def internal(request, error):
        logging.getLogger(__name__).error(
            "http_failed request_id=%s code=%s",
            request.state.request_id,
            type(error).__name__,
        )
        return failure(request, 500, "INTERNAL_ERROR", "请求处理失败，请通过请求 ID 排查。")

    def application(request: Request):
        return request.app.state.service

    Service = Annotated[ApplicationService, Depends(application)]

    @app.get("/health")
    def health(service: Service):
        return service.health()

    @app.get("/tickets", response_model=list[TicketView])
    def tickets(
        service: Service,
        principal: Identity,
        limit: PageLimit = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return service.tickets(principal, limit=limit, offset=offset)

    @app.post("/tickets", status_code=201, response_model=TicketView)
    def create_ticket(body: CreateTicket, service: Service, principal: Identity, key: RequestKey):
        return service.create_ticket(principal, key, body)

    @app.get("/tickets/{ticket_id}", response_model=TicketView)
    def ticket(ticket_id: Identifier, service: Service, principal: Identity):
        return service.ticket(ticket_id, principal)

    @app.post("/tickets/{ticket_id}/runs", status_code=202, response_model=Accepted)
    def start(
        ticket_id: Identifier,
        body: StartRun,
        service: Service,
        principal: Identity,
        key: RequestKey,
    ):
        return service.start_run(ticket_id, principal, key, body)

    @app.get("/runs/{run_id}", response_model=RunView)
    def run(run_id: Identifier, service: Service, principal: Identity):
        return service.run(run_id, principal)

    @app.get("/runs/{run_id}/events", response_model=EventPage)
    def events(
        run_id: Identifier,
        service: Service,
        principal: Identity,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: PageLimit = 100,
    ):
        return service.events(run_id, principal, after_seq=after_seq, limit=limit)

    @app.post("/runs/{run_id}/responses", status_code=202, response_model=Accepted)
    def respond(
        run_id: Identifier,
        body: PendingResponse,
        service: Service,
        principal: Identity,
        key: RequestKey,
    ):
        return service.respond(run_id, principal, key, body)

    @app.post("/runs/{run_id}/resume", status_code=202, response_model=Accepted)
    def resume(
        run_id: Identifier,
        body: EmptyRequest,
        service: Service,
        principal: Identity,
        key: RequestKey,
    ):
        return service.resume(run_id, principal, key)

    @app.post("/runs/{run_id}/cancel", status_code=202, response_model=Accepted)
    def cancel(
        run_id: Identifier,
        body: EmptyRequest,
        service: Service,
        principal: Identity,
        key: RequestKey,
    ):
        return service.cancel(run_id, principal, key)

    return app


app = create_app()
