import base64
from datetime import date, time, timedelta, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncEngine

from db import Base
import models

def _json_serial(obj: Any) -> str:
    if isinstance(obj, (date, time)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return str(obj.total_seconds())
    if isinstance(obj, Enum):
        return str(obj.value)
    if isinstance(obj, (Decimal, UUID)):
        return str(obj)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("utf-8")
    raise TypeError(f"Type {type(obj)} not serializable")


async def generate_backup(engine: AsyncEngine) -> bytes:
    tables_data: dict[str, list[dict[str, Any]]] = {}
    row_counts: dict[str, int] = {}

    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            result = await conn.execute(select(table))
            rows = [dict(row) for row in result.mappings()]
            tables_data[table.name] = rows
            row_counts[table.name] = len(rows)

    payload = {
        "meta": {
            "format" : 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dialect":  engine.dialect.name,
            "row_counts": row_counts,
        },
        "tables": tables_data
        }

    return json.dumps(payload, default=_json_serial, ensure_ascii=False).encode("utf-8")
