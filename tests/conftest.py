import os
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

# ВАЖНО: устанавливаем переменные ДО импорта db и config
os.environ["BOT_TOKEN"] = "test_token"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["ADMIN_IDS"] = "123456789"

from db import Base, engine

@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Создает таблицы в памяти перед всеми тестами и удаляет после."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)