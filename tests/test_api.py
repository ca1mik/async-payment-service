from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import Outbox, Payment


async def test_create_and_get_exact_decimal_and_metadata(client, payload, sessions):
    response = await client.post(
        "/api/v1/payments", json=payload, headers={"Idempotency-Key": "order-42"}
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "pending"
    detail = await client.get(f"/api/v1/payments/{data['payment_id']}")
    assert detail.status_code == 200
    assert detail.json()["amount"] == "123.45"
    assert detail.json()["metadata"] == payload["metadata"]
    assert detail.json()["processed_at"] is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        event = await session.scalar(select(Outbox))
        assert event.payload == {"payment_id": data["payment_id"], "attempt": 1}
        assert event.published_at is None


async def test_idempotent_replay_and_conflicting_body(client, payload, sessions):
    headers = {"Idempotency-Key": "same-order"}
    first = await client.post("/api/v1/payments", json=payload, headers=headers)
    same = await client.post("/api/v1/payments", json=payload, headers=headers)
    assert first.json()["payment_id"] == same.json()["payment_id"]
    assert first.json()["status"] == same.json()["status"]
    # SQLite strips timezone info; production PostgreSQL keeps TIMESTAMPTZ.
    assert datetime.fromisoformat(first.json()["created_at"]).replace(tzinfo=UTC) == (
        datetime.fromisoformat(same.json()["created_at"]).replace(tzinfo=UTC)
    )
    changed = await client.post(
        "/api/v1/payments", json={**payload, "amount": "999.00"}, headers=headers
    )
    assert changed.status_code == 409
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1


async def test_equivalent_decimal_and_json_key_order_are_idempotent(client, payload):
    headers = {"Idempotency-Key": "normalized"}
    first = await client.post("/api/v1/payments", json={**payload, "amount": "1"}, headers=headers)
    second = await client.post(
        "/api/v1/payments",
        json={
            "amount": "1.00",
            **{key: value for key, value in reversed(list(payload.items())) if key != "amount"},
        },
        headers=headers,
    )
    assert second.status_code == 202
    assert first.json()["payment_id"] == second.json()["payment_id"]


@pytest.mark.parametrize("path", ["/api/v1/payments/" + str(uuid4()), "/health", "/openapi.json"])
async def test_all_get_endpoints_require_key(client, path):
    assert (await client.get(path, headers={"X-API-Key": "incorrect"})).status_code == 401
    client.headers.pop("X-API-Key")
    assert (await client.get(path)).status_code == 401


async def test_create_requires_api_key_and_idempotency_key(client, payload):
    assert (await client.post("/api/v1/payments", json=payload)).status_code == 422
    client.headers.pop("X-API-Key")
    assert (
        await client.post("/api/v1/payments", json=payload, headers={"Idempotency-Key": "key"})
    ).status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "0"),
        ("amount", "-1"),
        ("amount", "1.001"),
        ("amount", "10000000000000000.00"),
        ("amount", "NaN"),
        ("currency", "GBP"),
        ("description", ""),
        ("webhook_url", "ftp://merchant.example/hook"),
        ("webhook_url", "https://user:password@merchant.example/hook"),
        ("unexpected", "field"),
    ],
)
async def test_invalid_requests_create_no_rows(client, sessions, payload, field, value):
    response = await client.post(
        "/api/v1/payments", json={**payload, field: value}, headers={"Idempotency-Key": "key"}
    )
    assert response.status_code == 422
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_not_found_and_malformed_uuid(client):
    assert (await client.get(f"/api/v1/payments/{uuid4()}")).status_code == 404
    assert (await client.get("/api/v1/payments/not-a-uuid")).status_code == 422
