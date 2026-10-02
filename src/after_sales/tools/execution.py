"""A bounded read executor. A deadline never turns a failed query into a business fact."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import TypeVar

from after_sales.tools.contracts import ErrorCode, ToolFailure

T = TypeVar("T")


class ReadExecutor:
    def __init__(self, *, timeout_seconds: float, workers: int = 2):
        self.timeout_seconds = timeout_seconds
        self._capacity = BoundedSemaphore(workers)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="after-sales-read")

    async def run(self, operation: Callable[[], T]) -> T:
        # No executor backlog: timed-out operations occupy a slot until the read actually finishes.
        if not self._capacity.acquire(blocking=False):
            raise ToolFailure(ErrorCode.TOOL_BUSY, "只读查询容量已满", retryable=True)
        try:
            future = self._pool.submit(operation)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _: self._capacity.release())
        wrapped = asyncio.wrap_future(future)
        # Consume late exceptions without creating evidence or changing the returned timeout.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), self.timeout_seconds)
        except TimeoutError as error:
            raise ToolFailure(ErrorCode.TOOL_TIMEOUT, "只读查询超时", retryable=True) from error

    def close(self) -> None:
        # Python cannot kill a running thread. Adapters also need their own finite I/O deadlines.
        self._pool.shutdown(wait=False, cancel_futures=True)
