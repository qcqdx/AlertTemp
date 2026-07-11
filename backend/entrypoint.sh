#!/bin/sh
# pg_isready проходит раньше, чем postgres после initdb готов принимать
# сессии, поэтому ждём фактической готовности сами — иначе alembic падает
# и контейнер уходит в restart-цикл на медленном сторадже.
set -e

python - <<'EOF'
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

url = os.environ.get(
    "COLDWATCH_DATABASE_URL",
    "postgresql+asyncpg://coldwatch:coldwatch@localhost:5432/coldwatch",
)


async def wait_for_db(attempts: int = 60, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print("database is ready", flush=True)
            return
        except Exception as exc:
            print(
                f"waiting for database ({attempt}/{attempts}): {exc.__class__.__name__}",
                flush=True,
            )
            await asyncio.sleep(delay)
        finally:
            await engine.dispose()
    sys.exit("database did not become ready in time")


asyncio.run(wait_for_db())
EOF

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
