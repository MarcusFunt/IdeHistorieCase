"""Run with DDU_TEST_DB=1 after alembic upgrade head against an isolated DB."""
import os

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.connection import engine, session_factory
from app.db.models import ServiceHeartbeat
from app.workers.main import record_heartbeat

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_insert_and_update():
    await record_heartbeat(False, "loading")
    await record_heartbeat(True, "model ready")
    async with session_factory() as session:
        result = await session.execute(
            select(ServiceHeartbeat).where(ServiceHeartbeat.service_name == "turn-worker")
        )
        row = result.scalar_one()
        assert row.inference_ready is True
        assert row.message == "model ready"
