from fastapi import FastAPI

app = FastAPI(
    title="AI Agents QA Demo",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}
