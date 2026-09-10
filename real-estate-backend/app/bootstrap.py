"""Create tables on startup (no dummy data needed for this demo)."""

from .db import Base, engine
from . import models  # noqa: F401 — register mappings before create_all


async def init_models() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
