"""Async SQLAlchemy engine used by API and worker."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.settings import get_settings

engine = create_async_engine(
    get_settings().database_url, pool_pre_ping=True, hide_parameters=True
)
session_factory = async_sessionmaker(engine, expire_on_commit=False)
