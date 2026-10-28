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
