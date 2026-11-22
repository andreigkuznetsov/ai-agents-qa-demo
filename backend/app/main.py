import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.database import Database, Settings
from app.readiness import Readiness


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = Database(Settings.from_env())
    readiness = Readiness(database)
    app.state.readiness = readiness
    try:
        yield
    finally:
        await readiness.close()
        await database.close()


app = FastAPI(
    title="AI Agents QA Demo",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


@app.get("/health/ready")
@app.get("/healthz")
async def ready(request: Request) -> JSONResponse:
    readiness: Readiness = request.app.state.readiness
    deadline = (
        asyncio.get_running_loop().time()
        + readiness.database.settings.readiness_timeout
    )
    check = asyncio.create_task(readiness.check(deadline))
    disconnect = asyncio.create_task(wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            (check, disconnect), return_when=asyncio.FIRST_COMPLETED
        )
        if check in done and check.result():
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable"}, status_code=503)
    finally:
        for task in (check, disconnect):
            if not task.done():
                task.cancel()
        await asyncio.gather(check, disconnect, return_exceptions=True)
