import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models import Outbox, Payment, utcnow
from app.outbox import OutboxRelay
from app.payments import create_payment
from app.processing import InvalidEvent, PaymentProcessor
from app.schemas import PaymentCreate, PaymentEvent


async def make_payment(sessions, payload):
    return await create_payment(sessions, PaymentCreate(**payload), str(uuid4()))


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_result_saved_webhook_delivered_and_redelivery_ignored(sessions, payload, status):
    payment = await make_payment(sessions, payload)
    gateway = AsyncMock()
    gateway.charge.return_value = status
    deliveries = []

    def webhook(request):
        deliveries.append(json.loads(request.content))
        assert request.headers["Idempotency-Key"] == f"payment:{payment.id}:result"
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as http:
        processor = PaymentProcessor(sessions, gateway, http)
        event = PaymentEvent(payment_id=payment.id, attempt=1)
        await processor.handle(event)
        await processor.handle(event)
    gateway.charge.assert_awaited_once()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == status
    assert deliveries[0]["metadata"] == payload["metadata"]
    async with sessions() as session:
        saved = await session.get(Payment, payment.id)
        assert saved.processed_at is not None
        assert saved.webhook_delivered_at is not None
        assert saved.attempts_completed == 1


async def test_three_failures_schedule_exponential_retry_then_dlq(sessions, payload):
    payment = await make_payment(sessions, payload)
    gateway = AsyncMock()
    gateway.charge.return_value = "succeeded"
    deliveries = []

    def webhook(request):
        deliveries.append(json.loads(request.content))
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as http:
        processor = PaymentProcessor(sessions, gateway, http, retry_base_seconds=2)
        for attempt in range(1, 4):
            before = datetime.now(UTC)
            event = PaymentEvent(payment_id=payment.id, attempt=attempt)
            await processor.handle(event)
            await processor.handle(event)  # A duplicate must not consume another attempt.
            async with sessions() as session:
                saved = await session.get(Payment, payment.id)
                assert saved.status == "succeeded"
                assert saved.webhook_delivered_at is None
                assert saved.attempts_completed == attempt
                destination = "payments.new" if attempt < 3 else "payments.dead"
                row = await session.scalar(
                    select(Outbox).where(
                        Outbox.event_key
                        == f"{payment.id}:"
                        + (f"process:{attempt + 1}" if attempt < 3 else "dead:3")
                    )
                )
                assert row.destination == destination
                if attempt < 3:
                    delta = row.available_at.replace(tzinfo=UTC) - before
                    assert 2 * 2 ** (attempt - 1) <= delta.total_seconds() < 2 * 2**attempt
                else:
                    assert row.payload["error"] == "HTTPStatusError: HTTP 503"
    gateway.charge.assert_awaited_once()
    assert len(deliveries) == 3
    assert len({body["event_id"] for body in deliveries}) == 1
    assert len({body["processed_at"] for body in deliveries}) == 1


async def test_webhook_timeout_then_success_keeps_financial_result(sessions, payload):
    payment = await make_payment(sessions, payload)
    gateway = AsyncMock()
    gateway.charge.return_value = "failed"
    count = 0

    def webhook(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("simulated", request=request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as http:
        processor = PaymentProcessor(sessions, gateway, http)
        await processor.handle(PaymentEvent(payment_id=payment.id, attempt=1))
        await processor.handle(PaymentEvent(payment_id=payment.id, attempt=2))
        await processor.handle(PaymentEvent(payment_id=payment.id, attempt=1))
    gateway.charge.assert_awaited_once()
    assert count == 2
    async with sessions() as session:
        saved = await session.get(Payment, payment.id)
        assert saved.status == "failed"
        assert saved.webhook_delivered_at is not None
        assert saved.last_error is None
        assert saved.attempts_completed == 2


async def test_gateway_infrastructure_error_retries_without_webhook(sessions, payload):
    payment = await make_payment(sessions, payload)
    gateway = AsyncMock()
    gateway.charge.side_effect = [TimeoutError(), "succeeded"]
    transport = httpx.MockTransport(lambda _: httpx.Response(204))
    async with httpx.AsyncClient(transport=transport) as http:
        processor = PaymentProcessor(sessions, gateway, http)
        await processor.handle(PaymentEvent(payment_id=payment.id, attempt=1))
        async with sessions() as session:
            saved = await session.get(Payment, payment.id)
            assert saved.status == "pending"
            assert saved.processed_at is None
            assert saved.attempts_completed == 1
        await processor.handle(PaymentEvent(payment_id=payment.id, attempt=2))
    assert gateway.charge.await_count == 2


async def test_unknown_and_out_of_sequence_events_rejected(sessions, payload):
    gateway = AsyncMock()
    async with httpx.AsyncClient() as http:
        processor = PaymentProcessor(sessions, gateway, http)
        with pytest.raises(InvalidEvent):
            await processor.handle(PaymentEvent(payment_id=uuid4(), attempt=1))
        payment = await make_payment(sessions, payload)
        with pytest.raises(InvalidEvent):
            await processor.handle(PaymentEvent(payment_id=payment.id, attempt=3))
    gateway.charge.assert_not_awaited()


async def test_outbox_publish_failure_rolls_back_and_later_recovers(sessions, payload):
    await make_payment(sessions, payload)
    broker = AsyncMock()
    broker.publish.side_effect = [ConnectionError("broker unavailable"), None]
    relay = OutboxRelay(sessions, broker)
    with pytest.raises(ConnectionError):
        await relay.publish_one()
    async with sessions() as session:
        assert (await session.scalar(select(Outbox))).published_at is None
    assert await relay.publish_one() is True
    assert await relay.publish_one() is False
    async with sessions() as session:
        row = await session.scalar(select(Outbox))
        assert row.published_at is not None
        assert broker.publish.call_args.kwargs["message_id"] == str(row.id)
        assert broker.publish.call_args.kwargs["persist"] is True
        assert broker.publish.call_args.kwargs["mandatory"] is True


async def test_outbox_does_not_publish_future_retries(sessions, payload):
    await make_payment(sessions, payload)
    async with sessions.begin() as session:
        row = await session.scalar(select(Outbox))
        row.available_at = utcnow() + timedelta(hours=1)
    broker = AsyncMock()
    assert await OutboxRelay(sessions, broker).publish_one() is False
    broker.publish.assert_not_awaited()


async def test_payment_and_outbox_rollback_together(sessions, payload):
    from sqlalchemy import event, func
    from sqlalchemy.orm import Session

    def fail_on_outbox(session, *_):
        if any(isinstance(row, Outbox) for row in session.new):
            raise RuntimeError("simulated outbox insert failure")

    event.listen(Session, "before_flush", fail_on_outbox)
    try:
        with pytest.raises(RuntimeError, match="outbox insert failure"):
            await make_payment(sessions, payload)
    finally:
        event.remove(Session, "before_flush", fail_on_outbox)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0
