import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import APIKeyHeader

from app.config import Settings
from app.database import make_database
from app.models import Payment
from app.payments import IdempotencyConflict, create_payment
from app.schemas import PaymentAccepted, PaymentCreate, PaymentDetail


def create_app(settings: Settings | None = None, sessions=None) -> FastAPI:
    settings = settings or Settings()
    engine = None
    if sessions is None:
        engine, sessions = make_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if engine is not None:
            await engine.dispose()

    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def authenticate(key: Annotated[str | None, Depends(key_header)]):
        expected = settings.api_key.get_secret_value().encode()
        if key is None or not secrets.compare_digest(key.encode(), expected):
            raise HTTPException(status_code=401, detail="Invalid API key")

    app = FastAPI(
        title="Async Payment Service",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authenticate)],
        lifespan=lifespan,
    )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi():
        return app.openapi()

    @app.get("/health")
    async def health():
        from sqlalchemy import text

        async with sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/api/v1/payments", status_code=202, response_model=PaymentAccepted)
    async def post_payment(
        body: PaymentCreate,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
    ):
        if not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
        try:
            payment = await create_payment(sessions, body, idempotency_key)
        except IdempotencyConflict:
            raise HTTPException(
                status_code=409, detail="Idempotency-Key already used with a different request"
            ) from None
        return PaymentAccepted(
            payment_id=payment.id, status=payment.status, created_at=payment.created_at
        )

    @app.get("/api/v1/payments/{payment_id}", response_model=PaymentDetail)
    async def get_payment(payment_id: UUID):
        async with sessions() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise HTTPException(status_code=404, detail="Payment not found")
            return PaymentDetail(
                payment_id=payment.id,
                status=payment.status,
                created_at=payment.created_at,
                amount=payment.amount,
                currency=payment.currency,
                description=payment.description,
                metadata=payment.extra_data,
                idempotency_key=payment.idempotency_key,
                webhook_url=payment.webhook_url,
                processed_at=payment.processed_at,
                webhook_delivered_at=payment.webhook_delivered_at,
                attempts_completed=payment.attempts_completed,
                last_error=payment.last_error,
            )

    return app
