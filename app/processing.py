import logging
from datetime import timedelta
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Outbox, Payment, utcnow
from app.payments import payment_event
from app.schemas import PaymentEvent

logger = logging.getLogger(__name__)


class Gateway(Protocol):
    async def charge(self, payment: Payment) -> str: ...


class InvalidEvent(Exception):
    """Unknown payment or an impossible attempt sequence; route to broker DLQ."""


class PaymentProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: Gateway,
        http: httpx.AsyncClient,
        retry_base_seconds: float = 1,
    ):
        self.sessions = sessions
        self.gateway = gateway
        self.http = http
        self.retry_base_seconds = retry_base_seconds

    async def _locked_payment(self, session: AsyncSession, event: PaymentEvent) -> Payment:
        payment = await session.scalar(
            select(Payment).where(Payment.id == event.payment_id).with_for_update()
        )
        if payment is None:
            raise InvalidEvent("Unknown payment")
        if event.attempt > payment.attempts_completed + 1:
            raise InvalidEvent("Out-of-sequence attempt")
        return payment

    @staticmethod
    def _finished(payment: Payment, event: PaymentEvent) -> bool:
        return (
            payment.webhook_delivered_at is not None or event.attempt <= payment.attempts_completed
        )

    async def handle(self, event: PaymentEvent) -> None:
        try:
            await self._process(event)
        except InvalidEvent:
            raise
        except Exception as exc:
            # If recording the failure itself fails (e.g. DB unavailable), propagate.
            # RabbitMQ must requeue, rather than ACK an event without a durable successor.
            await self._record_failure(event, exc)

    async def _process(self, event: PaymentEvent) -> None:
        # Commit the financial result BEFORE calling the remote webhook. A delivery
        # failure must never cause the gateway to run again for a terminal payment.
        async with self.sessions.begin() as session:
            payment = await self._locked_payment(session, event)
            if self._finished(payment, event):
                return
            if payment.status == "pending":
                payment.status = await self.gateway.charge(payment)
                if payment.status not in {"succeeded", "failed"}:
                    raise ValueError("Invalid gateway result")
                payment.processed_at = utcnow()

        async with self.sessions.begin() as session:
            payment = await self._locked_payment(session, event)
            if self._finished(payment, event):
                return
            event_id = f"payment:{payment.id}:result"
            response = await self.http.post(
                payment.webhook_url,
                json={
                    "event_id": event_id,
                    "event": "payment.processed",
                    "payment_id": str(payment.id),
                    "status": payment.status,
                    "amount": format(payment.amount, ".2f"),
                    "currency": payment.currency,
                    "description": payment.description,
                    "metadata": payment.extra_data,
                    "created_at": payment.created_at.isoformat(),
                    "processed_at": payment.processed_at.isoformat(),
                },
                headers={"Idempotency-Key": event_id},
            )
            # Redirects are failures too; never forward payloads to redirected hosts.
            response.raise_for_status()
            payment.webhook_delivered_at = utcnow()
            payment.attempts_completed = event.attempt
            payment.last_error = None

    async def _record_failure(self, event: PaymentEvent, exc: Exception) -> None:
        error = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            error += f": HTTP {exc.response.status_code}"
        async with self.sessions.begin() as session:
            payment = await self._locked_payment(session, event)
            if self._finished(payment, event):
                return
            payment.attempts_completed = event.attempt
            payment.last_error = error
            if event.attempt < 3:
                attempt = event.attempt + 1
                delay = self.retry_base_seconds * 2 ** (event.attempt - 1)
                session.add(
                    Outbox(
                        payment_id=payment.id,
                        event_key=f"{payment.id}:process:{attempt}",
                        destination="payments.new",
                        payload=payment_event(payment.id, attempt),
                        available_at=utcnow() + timedelta(seconds=delay),
                    )
                )
            else:
                session.add(
                    Outbox(
                        payment_id=payment.id,
                        event_key=f"{payment.id}:dead:3",
                        destination="payments.dead",
                        payload={**event.model_dump(mode="json"), "error": error},
                    )
                )
        logger.warning(
            "Attempt failed payment=%s attempt=%s error=%s", event.payment_id, event.attempt, error
        )
