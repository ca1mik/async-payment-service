import asyncio
import logging

from faststream.rabbit import RabbitBroker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Outbox, utcnow

logger = logging.getLogger(__name__)


class OutboxRelay:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], broker: RabbitBroker):
        self.sessions = sessions
        self.broker = broker

    async def publish_one(self) -> bool:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Outbox)
                .where(Outbox.published_at.is_(None), Outbox.available_at <= utcnow())
                .order_by(Outbox.available_at, Outbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            # Confirmed persistent publish happens before marking the DB row sent.
            # A crash between these steps can duplicate delivery, but cannot lose it.
            await self.broker.publish(
                row.payload,
                exchange="payments.dlx" if row.destination == "payments.dead" else "",
                routing_key=row.destination,
                persist=True,
                mandatory=True,
                message_id=str(row.id),
                timeout=5,
            )
            row.published_at = utcnow()
        return True

    async def run(self, stop: asyncio.Event, poll_seconds: float) -> None:
        while not stop.is_set():
            try:
                if await self.publish_one():
                    continue
            except Exception:
                logger.exception("Outbox publish failed; event remains pending")
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
