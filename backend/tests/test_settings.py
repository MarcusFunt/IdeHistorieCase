import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_password_with_reserved_characters_is_url_encoded():
    settings = Settings(
        db_host="postgres",
        db_user="ddu_app",
        db_password="p@ss:with/odd#chars",
        db_name="history",
    )
    url = settings.database_url.render_as_string(hide_password=False)
    assert "p%40ss%3Awith%2Fodd%23chars" in url
    assert "postgres:5432/history" in url


@pytest.mark.parametrize("stale_seconds", [45, 60, 64])
def test_stale_threshold_must_allow_poll_and_health_timeout(stale_seconds):
    with pytest.raises(ValidationError, match="worker_stale_seconds"):
        Settings(worker_poll_seconds=60, worker_stale_seconds=stale_seconds)


def test_stale_threshold_accepts_margin_after_health_timeout():
    settings = Settings(worker_poll_seconds=60, worker_stale_seconds=65)
    assert settings.worker_stale_seconds == 65
