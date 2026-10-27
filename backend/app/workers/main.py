"""M1 inference readiness monitor; durable turn processing arrives in M2."""
import asyncio
import logging

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.ai.llm import LlamaClient
from app.core.settings import get_settings
from app.db.connection import engine
from app.db.models import ServiceHeartbeat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def record_heartbeat(inference_ready: bool, message: str) -> None:
    table = ServiceHeartbeat.__table__
    statement = insert(table).values(
        service_name="turn-worker", inference_ready=inference_ready, message=message
    )
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.service_name],
        set_={
            "inference_ready": statement.excluded.inference_ready,
            "message": statement.excluded.message,
            "last_seen": func.now(),
        },
    )
    async with engine.begin() as connection:
        await connection.execute(statement)


async def run() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0), trust_env=False) as client:
        llm = LlamaClient(settings.llm_base_url, settings.llm_model_alias, client)
        while True:
            ready = await llm.is_ready()
            message = "model ready" if ready else "model loading or unreachable"
            try:
                await record_heartbeat(ready, message)
            except Exception:
                logger.exception("Could not persist worker heartbeat; retrying")
            else:
                logger.info("worker heartbeat: inference_ready=%s", ready)
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Worker stopped")
