"""Opt in with RUN_INTEGRATION=1; run against the disposable Compose stack."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

import aio_pika
import httpx
import pytest
from sqlalchemy import func, select, text

from app.config import Settings
from app.database import make_database
from app.models import Outbox, Payment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Requires PostgreSQL/RabbitMQ"),
]


@pytest.fixture
def receiver():
    deliveries = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                deliveries.append((self.path, body, self.headers["Idempotency-Key"]))
            self.send_response(503 if self.path.startswith("/failure") else 204)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = os.getenv("TEST_WEBHOOK_HOST", "host.docker.internal")
    yield f"http://{host}:{server.server_port}", deliveries
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
async def remote():
    settings = Settings()
    async with httpx.AsyncClient(
        base_url=os.getenv("TEST_API_URL", "http://localhost:8000"),
        headers={"X-API-Key": settings.api_key.get_secret_value()},
        timeout=20,
        trust_env=False,
    ) as client:
        yield client


async def wait_payment(client, payment_id, predicate):
    async with asyncio.timeout(60):
        while True:
            response = await client.get(f"/api/v1/payments/{payment_id}")
            response.raise_for_status()
            if predicate(response.json()):
                return response.json()
            await asyncio.sleep(0.2)


async def test_real_stack_concurrent_idempotency_and_webhook(remote, receiver, payload):
    url, deliveries = receiver
    body = {**payload, "webhook_url": f"{url}/success"}
    key = str(uuid4())
    responses = await asyncio.gather(
        *[
            remote.post("/api/v1/payments", json=body, headers={"Idempotency-Key": key})
            for _ in range(12)
        ]
    )
    assert all(response.status_code == 202 for response in responses)
    ids = {response.json()["payment_id"] for response in responses}
    assert len(ids) == 1
    payment_id = ids.pop()
    detail = await wait_payment(remote, payment_id, lambda row: row["webhook_delivered_at"])
    assert detail["status"] in {"succeeded", "failed"}
    assert detail["attempts_completed"] == 1
    assert len(deliveries) == 1
    assert deliveries[0][1]["payment_id"] == payment_id
    engine, sessions = make_database(Settings().database_url)
    try:
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Payment).where(Payment.idempotency_key == key)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(Outbox.event_key == f"{payment_id}:process:1")
                )
                == 1
            )
    finally:
        await engine.dispose()


async def test_real_stack_three_attempts_publish_to_dead_exchange(remote, receiver, payload):
    url, deliveries = receiver
    connection = await aio_pika.connect_robust(Settings().rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        exchange = await channel.declare_exchange("payments.dlx", durable=True)
        observer = await channel.declare_queue(exclusive=True, auto_delete=True)
        await observer.bind(exchange, routing_key="payments.dead")
        response = await remote.post(
            "/api/v1/payments",
            json={**payload, "webhook_url": f"{url}/failure"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202
        payment_id = response.json()["payment_id"]
        detail = await wait_payment(remote, payment_id, lambda row: row["attempts_completed"] == 3)
        assert detail["webhook_delivered_at"] is None
        assert detail["status"] in {"succeeded", "failed"}
        assert len(deliveries) == 3
        assert len({body["processed_at"] for _, body, _ in deliveries}) == 1
        assert len({key for _, _, key in deliveries}) == 1
        async with asyncio.timeout(15):
            while True:
                message = await observer.get(fail=False)
                if message is not None:
                    body = json.loads(message.body)
                    await message.ack()
                    if body["payment_id"] == payment_id:
                        assert body["attempt"] == 3
                        assert body["error"] == "HTTPStatusError: HTTP 503"
                        break
                await asyncio.sleep(0.1)


async def test_postgres_unique_constraint_and_skip_locked():
    """Exercise real PG locking inside an isolated schema; no production rows touched."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, utcnow
    from app.payments import create_payment
    from app.schemas import PaymentCreate

    schema = "test_" + uuid4().hex
    url = Settings().database_url
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        body = PaymentCreate(
            amount="1.00",
            currency="RUB",
            description="lock test",
            webhook_url="https://merchant.example/webhook",
        )
        results = await asyncio.gather(*[create_payment(sessions, body, "same") for _ in range(8)])
        assert len({row.id for row in results}) == 1
        payment_id = UUID(str(results[0].id))
        async with sessions.begin() as first, sessions.begin() as second:
            locked = await first.scalar(
                select(Outbox)
                .where(Outbox.payment_id == payment_id, Outbox.available_at <= utcnow())
                .with_for_update(skip_locked=True)
            )
            assert locked is not None
            skipped = await second.scalar(select(Outbox).with_for_update(skip_locked=True))
            assert skipped is None
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
