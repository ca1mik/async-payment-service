from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from faststream.rabbit import TestRabbitBroker
from faststream.rabbit.message import RabbitMessage

from app.messaging import PAYMENTS_QUEUE, make_broker
from app.schemas import PaymentEvent
from app.worker import register_consumer


async def test_faststream_injects_raw_message_and_dispatches_event():
    broker = make_broker("amqp://guest:guest@localhost/")
    processor = AsyncMock()
    register_consumer(broker, processor)
    event = PaymentEvent(payment_id=uuid4(), attempt=1)
    async with TestRabbitBroker(broker):
        await broker.publish(event.model_dump(mode="json"), queue=PAYMENTS_QUEUE)
    processor.handle.assert_awaited_once_with(event)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        {"payment_id": "bad"},
        {
            "payment_id": str(uuid4()),
            "attempt": 4,
        },
    ],
)
async def test_poison_messages_do_not_reach_processor(body):
    broker = make_broker("amqp://guest:guest@localhost/")
    processor = AsyncMock()
    register_consumer(broker, processor)
    async with TestRabbitBroker(broker):
        await broker.publish(body, queue=PAYMENTS_QUEUE)
    processor.handle.assert_not_awaited()


async def test_invalid_payment_is_rejected_without_requeue(monkeypatch):
    from app.processing import InvalidEvent

    rejected = []
    original = RabbitMessage.reject

    async def reject(message, **kwargs):
        rejected.append(kwargs)
        await original(message, **kwargs)

    monkeypatch.setattr(RabbitMessage, "reject", reject)
    broker = make_broker("amqp://guest:guest@localhost/")
    processor = AsyncMock()
    processor.handle.side_effect = InvalidEvent("unknown payment")
    register_consumer(broker, processor)
    async with TestRabbitBroker(broker):
        await broker.publish({"payment_id": str(uuid4()), "attempt": 1}, queue=PAYMENTS_QUEUE)
    assert rejected == [{"requeue": False}]


async def test_infrastructure_failure_nacks_instead_of_losing_message(monkeypatch):
    nacked = []
    original = RabbitMessage.nack

    async def nack(message, **kwargs):
        nacked.append(kwargs)
        await original(message, **kwargs)

    monkeypatch.setattr(RabbitMessage, "nack", nack)
    broker = make_broker("amqp://guest:guest@localhost/")
    processor = AsyncMock()
    processor.handle.side_effect = ConnectionError("database unavailable")
    register_consumer(broker, processor)
    async with TestRabbitBroker(broker):
        with pytest.raises(ConnectionError):
            await broker.publish({"payment_id": str(uuid4()), "attempt": 1}, queue=PAYMENTS_QUEUE)
    assert nacked == [{}]  # RabbitMessage.nack defaults to requeue=True.
