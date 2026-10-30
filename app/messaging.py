from faststream.rabbit import Channel, RabbitBroker, RabbitExchange, RabbitQueue

DEAD_EXCHANGE = RabbitExchange("payments.dlx", durable=True)
DEAD_QUEUE = RabbitQueue("payments.dead", durable=True, routing_key="payments.dead")
PAYMENTS_QUEUE = RabbitQueue(
    "payments.new",
    durable=True,
    arguments={
        "x-dead-letter-exchange": "payments.dlx",
        "x-dead-letter-routing-key": "payments.dead",
    },
)


def make_broker(url: str) -> RabbitBroker:
    return RabbitBroker(
        url,
        default_channel=Channel(
            prefetch_count=1,
            publisher_confirms=True,
            on_return_raises=True,
        ),
        graceful_timeout=30,
    )


async def declare_topology(broker: RabbitBroker) -> None:
    exchange = await broker.declare_exchange(DEAD_EXCHANGE)
    queue = await broker.declare_queue(DEAD_QUEUE)
    await queue.bind(exchange, routing_key="payments.dead")
    await broker.declare_queue(PAYMENTS_QUEUE)
