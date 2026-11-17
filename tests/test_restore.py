import pytest
import json
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine
from db import Base
from backup_db import generate_backup
from restore_db import restore_backup
from models import Game
import models

@pytest.mark.asyncio
async def test_backup_restore_round_trip():
    # Используем отдельный движок в памяти, чтобы не ломать основной conftest
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    # Создаем тестовую игру
    from sqlalchemy.ext.asyncio import async_sessionmaker
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add(Game(title="Тестовая игра", when=datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)))
        await s.commit()
        
    # 1. Делаем бэкап
    backup_bytes = await generate_backup(engine)
    data = json.loads(backup_bytes)
    assert data["meta"]["row_counts"]["games"] == 1
    
    # 2. Чистим базу
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        
    # 3. Восстанавливаем
    await restore_backup(engine, backup_bytes)
    
    # 4. Проверяем
    async with Session() as s:
        from sqlalchemy import select
        res = await s.execute(select(Game))
        games = res.scalars().all()
        assert len(games) == 1
        assert games[0].title == "Тестовая игра"