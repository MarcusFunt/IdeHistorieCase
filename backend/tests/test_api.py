import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_liveness_when_db_is_offline(monkeypatch):
    async def down():
        return False

    monkeypatch.setattr("app.main.database_ready", down)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/healthz")).json() == {"status": "alive"}
        response = await c.get("/readyz")
        assert response.status_code == 503
        assert response.json()["database"] is False


@pytest.mark.asyncio
async def test_ready_with_healthy_database(monkeypatch):
    async def up():
        return True

    monkeypatch.setattr("app.main.database_ready", up)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
