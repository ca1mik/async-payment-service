from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Outbox, Payment
from app.schemas import PaymentCreate


class IdempotencyConflict(Exception):
    pass


def payment_event(payment_id: UUID, attempt: int) -> dict:
    return {"payment_id": str(payment_id), "attempt": attempt}


async def create_payment(
    sessions: async_sessionmaker[AsyncSession], body: PaymentCreate, key: str
) -> Payment:
    fingerprint = body.fingerprint()
    async with sessions() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(Payment).where(Payment.idempotency_key == key)
                )
                if existing is not None:
                    if existing.request_hash != fingerprint:
                        raise IdempotencyConflict
                    return existing
                payment = Payment(
                    id=uuid4(),
                    amount=body.amount,
                    currency=body.currency,
                    description=body.description,
                    extra_data=body.metadata,
                    webhook_url=str(body.webhook_url),
                    idempotency_key=key,
                    request_hash=fingerprint,
                )
                session.add(payment)
                await session.flush()
                session.add(
                    Outbox(
                        payment_id=payment.id,
                        event_key=f"{payment.id}:process:1",
                        destination="payments.new",
                        payload=payment_event(payment.id, 1),
                    )
                )
            return payment
        except IntegrityError:
            # A concurrent request can win the unique-key race after the initial SELECT.
            await session.rollback()
            existing = await session.scalar(select(Payment).where(Payment.idempotency_key == key))
            if existing is None:
                raise
            if existing.request_hash != fingerprint:
                raise IdempotencyConflict from None
            return existing
