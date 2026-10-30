import asyncio
import random

from app.models import Payment


class SimulatedGateway:
    def __init__(self, min_seconds: float = 2, max_seconds: float = 5):
        if min_seconds > max_seconds:
            raise ValueError("Gateway minimum delay must not exceed maximum delay")
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds

    async def charge(self, payment: Payment) -> str:
        await asyncio.sleep(random.uniform(self.min_seconds, self.max_seconds))
        return "succeeded" if random.random() < 0.9 else "failed"
