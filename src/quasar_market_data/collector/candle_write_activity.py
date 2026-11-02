"""A signal that a candle write is queued or running, for open interest to yield to (ADR-0003 P-9 rule 2).

The candle path only enters and leaves ``writing()`` around each write; it never waits for open interest. The open
interest writer checks ``active`` immediately before each transaction and waits with ``wait_idle()`` while it is
true. ``wait_idle()`` returning does not mean the activity is still idle when the waiter runs (a new candle write
may have entered in between), so a caller that must be sure loops: ``while activity.active: await
activity.wait_idle()``.

Create and use it inside one running event loop; it is not thread-safe and needs not be (both writers are driven
from the loop, their SQLite work runs on their own threads).
"""

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager


class CandleWriteActivity:
    def __init__(self) -> None:
        self._count = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active(self) -> bool:
        return self._count > 0

    def writing(self) -> AbstractContextManager[None]:
        """Mark one candle write as queued or running for the duration of the ``with`` block."""
        return self._writing()

    @contextmanager
    def _writing(self) -> Iterator[None]:
        self._count += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    async def wait_idle(self) -> None:
        """Return when no candle write is active; at once if none is."""
        await self._idle.wait()
