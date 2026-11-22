import asyncio
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from app.database import Database


@dataclass
class Probe:
    task: asyncio.Task[bool]
    waiters: int = 0
    stopping: bool = False


class Readiness:
    def __init__(self, database: Database):
        self.database = database
        self.probe: Probe | None = None

    async def _check_database(self, deadline: float) -> bool:
        try:
            async with asyncio.timeout_at(deadline), self.database.transaction():
                pass
        except (TimeoutError, SQLAlchemyError, RuntimeError, OSError):
            return False
        finally:
            await self.database.drain_cleanup()
        return True

    async def check(self, deadline: float | None = None) -> bool:
        if not self.database.settings.password:
            return False
        if deadline is None:
            deadline = (
                asyncio.get_running_loop().time()
                + self.database.settings.readiness_timeout
            )
        probe = self.probe
        if probe is None or probe.task.done():
            if self.database.invalidating:
                return False
            probe = Probe(asyncio.create_task(self._check_database(deadline)))
            self.probe = probe
        if probe.stopping:
            return False
        probe.waiters += 1
        try:
            async with asyncio.timeout_at(deadline):
                return await asyncio.shield(probe.task)
        except TimeoutError:
            return False
        finally:
            probe.waiters -= 1
            if probe.waiters == 0 and not probe.task.done() and not probe.stopping:
                probe.stopping = True
                probe.task.cancel()

    async def drain(self) -> None:
        if self.probe is not None:
            await asyncio.gather(
                asyncio.shield(self.probe.task), return_exceptions=True
            )
        await self.database.drain_cleanup()

    async def close(self) -> None:
        if (
            self.probe is not None
            and not self.probe.task.done()
            and not self.probe.stopping
        ):
            self.probe.stopping = True
            self.probe.task.cancel()
        await self.drain()
