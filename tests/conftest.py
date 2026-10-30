import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_app
from app.config import Settings
from app.models import Base


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        api_key="test-api-key-at-least-16",
        database_url="sqlite+aiosqlite://",
        rabbitmq_url="amqp://guest:guest@localhost/",
    )


@pytest.fixture
async def client(sessions, settings):
    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": settings.api_key.get_secret_value()},
    ) as client:
        yield client


@pytest.fixture
def payload():
    return {
        "amount": "123.45",
        "currency": "RUB",
        "description": "Test order",
        "metadata": {"order_id": 42, "nested": {"ok": True}},
        "webhook_url": "https://merchant.example/webhook",
    }
