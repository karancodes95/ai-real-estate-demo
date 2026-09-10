"""Per-IP daily chat rate limit, backed by Postgres.

Persisting to the DB (not memory) means counts survive restarts, so an IP that
abuses the demo day after day stays visible in the ``ip_usage`` table.

Concurrency: the count is a read-modify-write, so it must be atomic or two
concurrent requests could both slip past the cap (or collide on the unique
constraint). We first ensure today's row exists with an atomic
``INSERT ... ON CONFLICT DO NOTHING``, then ``SELECT ... FOR UPDATE`` to lock it
before deciding — correct even with multiple uvicorn workers.

Call :func:`check_and_count` exactly once per chat request.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .config import settings
from .db import SessionLocal
from .models import IpUsage


async def check_and_count(ip: str) -> bool:
    """Record one chat attempt from ``ip`` for today.

    Returns ``True`` if within the daily cap (allow), ``False`` if the cap is
    already reached (block). A blocked attempt is still logged (``blocked``++)
    so repeat offenders are queryable. A cap of 0 means unlimited.
    """
    cap = settings.chat_daily_ip_cap
    today = date.today()
    now = datetime.utcnow()
    async with SessionLocal() as s:
        async with s.begin():
            # 1) Ensure today's row exists — atomic, no-op if another request
            #    already created it.
            await s.execute(
                pg_insert(IpUsage)
                .values(ip=ip, day=today, count=0, blocked=0, first_seen=now, last_seen=now)
                .on_conflict_do_nothing(index_elements=["ip", "day"])
            )
            # 2) Lock the row so the decide-and-increment below can't race.
            row = (
                await s.execute(
                    select(IpUsage)
                    .where(IpUsage.ip == ip, IpUsage.day == today)
                    .with_for_update()
                )
            ).scalar_one()
            row.last_seen = now
            if cap > 0 and row.count >= cap:
                row.blocked += 1
                return False
            row.count += 1
            return True
