import taskiq_fastapi
from taskiq import AsyncBroker, InMemoryBroker, SimpleRetryMiddleware
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.core.config import get_settings

_settings = get_settings()

broker: AsyncBroker
if _settings.environment =="test":
    broker = InMemoryBroker()
else:
    # Redis Streams give acknowledgements => at-least-once delivery. Tasks MUST be idempotent.
    broker = RedisStreamBroker(url=str(_settings.redis_broker_url)).with_result_backend(
        RedisAsyncResultBackend(redis_url=str(_settings.redis_broker_url), result_ex_time=3600)
    )

broker.add_middlewares(SimpleRetryMiddleware(default_retry_count=3))

# Lazy string path: the worker imports the FastAPI app (and runs its lifespan) itself.
taskiq_fastapi.init(broker, "app.main:app")