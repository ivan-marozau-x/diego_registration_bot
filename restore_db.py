import json
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import insert, text
from db import Base
import models

async def restore_backup(engine: AsyncEngine, json_bytes: bytes):
    data = json.loads(json_bytes.decode("utf-8"))
    async with engine.begin() as conn:
        for table_name, rows in data["tables"].items():
            if not rows:
                continue
            table = Base.metadata.tables[table_name]
            await conn.execute(insert(table), rows)
            
            # Сброс счетчика ID для Postgres
            if engine.dialect.name == "postgresql":
                await conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table_name}"
                ))