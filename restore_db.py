import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import insert, text, select, func
from db import Base
from config import settings
import models 

def _parse_datetime(val: str, col_name: str) -> datetime:
    dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        if col_name == "when":
            dt = dt.replace(tzinfo=ZoneInfo(settings.tz))
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _coerce_row(table, raw: dict) -> dict:
    out = {}
    for col in table.columns:
        if col.name not in raw:
            continue
        val = raw[col.name]
        if val is None:
            out[col.name] = None
            continue
        if col.type.python_type is datetime and isinstance(val, str):
            out[col.name] = _parse_datetime(val, col.name)
        else:
            out[col.name] = val
    return out

async def restore_backup(engine: AsyncEngine, json_bytes: bytes):
    """Restores DB from JSON. Reguires EMPTY database."""
    data = json.loads(json_bytes.decode("utf-8"))
    is_postgres = engine.dialect.name == "postgresql"

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Проверка на пустоту
        for table in Base.metadata.sorted_tables:
            res = await conn.execute(select(1).select_from(table).limit(1))
            if res.first() is not None:
                raise ValueError(f"БД не пуста! Таблица '{table.name}' содержит данные.")

        # Восстановление
        for table in Base.metadata.sorted_tables:
            raw_rows = data.get("tables", {}).get(table.name, [])
            if not raw_rows:
                continue
            
            rows = [_coerce_row(table, r) for r in raw_rows]
            await conn.execute(insert(table), rows)
            
            # Сверка row_counts (если в файле есть мета)
            if "row_counts" in data.get("meta", {}):
                expected = data["meta"]["row_counts"].get(table.name, 0)
                actual = await conn.scalar(select(func.count()).select_from(table))
                if expected != actual:
                    raise ValueError(f"Ошибка: в таблице {table.name} ожидалось {expected} строк, вставлено {actual}.")

            # Сброс sequence
            if is_postgres:
                seq_name = await conn.scalar(text(f"SELECT pg_get_serial_sequence('{table.name}', 'id')"))
                if seq_name:
                    await conn.execute(text(
                        f"SELECT setval('{seq_name}', (SELECT COALESCE(MAX(id), 1) FROM {table.name}), true)"
                    ))      