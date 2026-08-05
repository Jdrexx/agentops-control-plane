from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from redis import Redis
from redis.exceptions import RedisError


class RunQueue:
    """Durable Redis queue with an in-process fallback for local development."""

    def __init__(self, handler: Callable[[int], None], workers: int = 4, consume: bool = True):
        self.handler = handler
        self.redis_url = os.getenv("REDIS_URL")
        self.redis = Redis.from_url(self.redis_url) if self.redis_url else None
        self.local = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agentops-run")
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        if self.redis is not None:
            self.redis.ping()
        if self.redis is not None and consume:
            for index in range(workers):
                thread = threading.Thread(
                    target=self._consume, name=f"agentops-redis-worker-{index}", daemon=True
                )
                thread.start()
                self.threads.append(thread)

    @property
    def backend(self) -> str:
        return "redis" if self.redis is not None else "local"

    def ready(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(self.redis.ping())
        except RedisError:
            return False

    def submit(self, run_id: int) -> None:
        if self.redis is None:
            self.local.submit(self.handler, run_id)
            return
        self.redis.lpush("agentops:runs", run_id)

    def _consume(self) -> None:
        assert self.redis is not None
        while not self.stop_event.is_set():
            try:
                item = self.redis.brpop("agentops:runs", timeout=1)
                if item is not None:
                    self.handler(int(item[1]))
            except (RedisError, ValueError):
                self.stop_event.wait(1)

    def close(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.local.shutdown(wait=True, cancel_futures=False)
        if self.redis is not None:
            self.redis.close()
