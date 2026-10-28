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


@pytest.mark.parametrize("base_url", ["", "   ", "ftp://llm:8080", "//llm:8080", "http://"])
def test_inference_url_must_be_a_nonempty_http_url(base_url):
    with pytest.raises(ValidationError, match="llm_base_url"):
        Settings(llm_base_url=base_url)


def test_inference_url_rejects_embedded_credentials():
    with pytest.raises(ValidationError, match="credentials"):
        Settings(llm_base_url="http://user:password@llm:8080")


def test_inference_model_alias_must_not_be_empty():
    with pytest.raises(ValidationError, match="llm_model_alias"):
        Settings(llm_model_alias=" ")


def test_model_specific_reasoning_default_is_disabled_but_configurable():
    assert Settings().llm_enable_thinking is False
    assert Settings(llm_enable_thinking=True).llm_enable_thinking is True
