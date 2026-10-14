"""Keep synchronous database work off the HTTP interaction event loop.

Each polling worker retains one serial loop and its own sessions/clients.
Cancellation is forwarded to that loop and shutdown waits for it to finish.
"""
import asyncio
import functools
import threading


async def run_isolated(factory):
    stopping = threading.Event()
    lock = threading.Lock()
    state = {}

    async def invoke():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with lock:
            state.update(loop=loop, task=task)
            cancelled = stopping.is_set()
        if cancelled:
            return
        try:
            return await factory()
        except asyncio.CancelledError:
            if not stopping.is_set():
                raise
        finally:
            with lock:
                state.clear()

    pending = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(invoke())))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        stopping.set()
        with lock:
            if state:
                state['loop'].call_soon_threadsafe(state['task'].cancel)
        try:
            await asyncio.shield(pending)
        finally:
            raise


def isolated_io(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        return await run_isolated(lambda: function(*args, **kwargs))
    return wrapper
