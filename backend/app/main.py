"""M1 API: process liveness and independent database/worker readiness."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.core.settings import get_settings
from app.db.connection import engine, session_factory
from app.db.models import ServiceHeartbeat

app = FastAPI(title="DDU History Game Backend", version="0.1.0")


async def database_ready() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # A database outage must yield 503 instead of a crash.
        return False
    return True


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness works independently of PostgreSQL and inference."""
    return {"status": "alive"}


@app.get("/readyz")
async def readyz():
    """Readiness requires database access, not the separate inference deployment."""
    if not await database_ready():
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": False})
    return {"status": "ready", "database": True}


@app.get("/api/v1/status")
async def status():
    """Private diagnostics for M1; do not expose to an unauthenticated public gateway."""
    if not await database_ready():
        return JSONResponse(status_code=503, content={"database": False, "worker": "unknown"})

    settings = get_settings()
    try:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ServiceHeartbeat).where(ServiceHeartbeat.service_name == "turn-worker")
                )
            ).scalar_one_or_none()
    except Exception:
        return JSONResponse(status_code=503, content={"database": True, "worker": "unknown"})

    if row is None:
        return {"database": True, "worker": "not_started", "inference": "unknown"}

    age = (datetime.now(timezone.utc) - row.last_seen).total_seconds()
    stale = age > settings.worker_stale_seconds
    return {
        "database": True,
        "worker": "stale" if stale else "running",
        "inference": "unknown" if stale else ("ready" if row.inference_ready else "degraded"),
        "worker_last_seen": row.last_seen.isoformat(),
        "inference_detail": row.message if not stale else "worker heartbeat expired",
    }
