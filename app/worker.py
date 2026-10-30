import asyncio
import logging
import signal

import httpx
from faststream.middlewares import AckPolicy
from faststream.rabbit import RabbitBroker, RabbitMessage
from pydantic import ValidationError

from app.config import Settings
from app.database import make_database
from app.gateway import SimulatedGateway
from app.messaging import PAYMENTS_QUEUE, declare_topology, make_broker
from app.outbox import OutboxRelay
from app.processing import InvalidEvent, PaymentProcessor
from app.schemas import PaymentEvent

logger = logging.getLogger(__name__)


def register_consumer(broker: RabbitBroker, processor: PaymentProcessor):
    @broker.subscriber(PAYMENTS_QUEUE, ack_policy=AckPolicy.NACK_ON_ERROR, no_reply=True)
    async def consume(message: RabbitMessage):
        try:
            event = PaymentEvent.model_validate_json(message.body)
        except ValidationError:
            logger.warning("Malformed event rejected to DLQ message=%s", message.message_id)
            await message.reject(requeue=False)
            return
        try:
            await processor.handle(event)
        except InvalidEvent:
            logger.warning("Invalid event rejected to DLQ payment=%s", event.payment_id)
            await message.reject(requeue=False)
        except Exception:
            # Broker requeues after an infrastructure failure; business retries have
            # already been persisted by PaymentProcessor. Avoid a tight outage loop.
            logger.exception("Consumer infrastructure error payment=%s", event.payment_id)
            await asyncio.sleep(1)
            raise

    return consume


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine, sessions = make_database(settings.database_url)
    broker = make_broker(settings.rabbitmq_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        async with httpx.AsyncClient(
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            processor = PaymentProcessor(
                sessions,
                SimulatedGateway(settings.gateway_min_seconds, settings.gateway_max_seconds),
                http,
                settings.retry_base_seconds,
            )
            register_consumer(broker, processor)
            try:
                await broker.connect()
                await declare_topology(broker)
                await broker.start()
                # Relay and the sole payment handler live in the same consumer process.
                await OutboxRelay(sessions, broker).run(stop, settings.outbox_poll_seconds)
            finally:
                await broker.stop()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
