import asyncio
from collections.abc import Callable
from typing import Any


async def run_blocking[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Keep ownership of blocking work until it finishes, even on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                # Retrieve a possible worker exception before propagating cancellation.
                if not task.cancelled():
                    task.exception()
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
