import asyncio
import threading
import time
import pytest
from app.background_io import run_isolated, isolated_io


def test_blocking_worker_does_not_hold_http_loop():
    started=threading.Event()
    release=threading.Event()
    async def slow():
        started.set()
        release.wait(2)
        return 42
    async def check():
        job=asyncio.create_task(run_isolated(slow))
        deadline=time.monotonic()+2
        while not started.is_set() and time.monotonic()<deadline:
            await asyncio.sleep(.005)
        assert started.is_set()
        before=time.monotonic()
        await asyncio.sleep(.02)
        assert time.monotonic()-before<.3
        release.set()
        assert await job==42
    try:
        asyncio.run(check())
    finally:
        release.set()


def test_shutdown_cancels_worker_and_waits_for_cleanup():
    started=threading.Event()
    stopped=threading.Event()
    async def worker():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(.01)
            stopped.set()
    async def check():
        task=asyncio.create_task(run_isolated(worker))
        while not started.is_set():
            await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    asyncio.run(check())


def test_one_session_thread_and_error_propagation():
    @isolated_io
    async def operation():
        first=threading.get_ident()
        await asyncio.sleep(.01)
        assert threading.get_ident()==first
        raise ValueError('expected')
    async def check():
        with pytest.raises(ValueError,match='expected'):
            await operation()
    asyncio.run(check())
