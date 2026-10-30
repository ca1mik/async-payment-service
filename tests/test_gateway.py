from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import SimulatedGateway


@pytest.mark.parametrize(("random_value", "expected"), [(0.899, "succeeded"), (0.9, "failed")])
async def test_gateway_probability_threshold_and_default_delay(random_value, expected):
    with (
        patch("app.gateway.random.uniform", return_value=3.5) as delay,
        patch("app.gateway.random.random", return_value=random_value),
        patch("app.gateway.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        assert await SimulatedGateway().charge(None) == expected
    delay.assert_called_once_with(2, 5)
    sleep.assert_awaited_once_with(3.5)
