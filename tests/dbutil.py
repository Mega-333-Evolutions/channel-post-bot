"""Tests run on SQLite by default; set TEST_DATABASE_URL to run the very same tests on PostgreSQL."""
import os

from app.db import Base, Database


def db_url_for(tmp_path, name="t.db"):
    return os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/{name}"


async def fresh_db(url: str) -> Database:
    db = Database(url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await db.init()
    return db
