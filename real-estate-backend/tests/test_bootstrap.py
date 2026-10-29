"""Startup's create_all (app.bootstrap.init_models) on a database that already
holds the old schema (leads, email_log, chat_messages and ip_usage, as on main
before sessions and events, with a row each) adds only sessions and events and
their indexes: the old tables' columns, constraints, indexes and rows are
untouched, and a second boot issues no DDL at all. Runs on its own throwaway
database, never the shared test DB or a live one."""

import asyncpg
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import bootstrap
from conftest import PG_HOST, PG_PORT, PG_USER

LEGACY_DB = "lead_to_lease_test_legacy"
OLD_TABLES = ["chat_messages", "email_log", "ip_usage", "leads"]

# main's schema before this change, as its create_all wrote it.
OLD_SCHEMA = [
    "CREATE TABLE chat_messages ( id SERIAL NOT NULL, session_id VARCHAR(100), "
    "role VARCHAR(20) NOT NULL, content TEXT NOT NULL, client_ip VARCHAR(60) NOT NULL, "
    "created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id) )",
    "CREATE TABLE email_log ( id SERIAL NOT NULL, lead_id INTEGER NOT NULL, "
    "to_email VARCHAR(180) NOT NULL, subject VARCHAR(200) NOT NULL, "
    "status VARCHAR(30) NOT NULL, detail VARCHAR(300) NOT NULL, "
    "created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id) )",
    "CREATE TABLE ip_usage ( id SERIAL NOT NULL, ip VARCHAR(60) NOT NULL, "
    "day DATE NOT NULL, count INTEGER NOT NULL, blocked INTEGER NOT NULL, "
    "first_seen TIMESTAMP WITHOUT TIME ZONE NOT NULL, "
    "last_seen TIMESTAMP WITHOUT TIME ZONE NOT NULL, PRIMARY KEY (id), UNIQUE (ip, day) )",
    "CREATE TABLE leads ( id SERIAL NOT NULL, name VARCHAR(120) NOT NULL, "
    "email VARCHAR(180) NOT NULL, phone VARCHAR(40) NOT NULL, budget VARCHAR(120) NOT NULL, "
    "location VARCHAR(120) NOT NULL, property_type VARCHAR(80) NOT NULL, "
    "timeline_months INTEGER NOT NULL, financing VARCHAR(40) NOT NULL, "
    "intent VARCHAR(40) NOT NULL, score INTEGER NOT NULL, grade VARCHAR(20) NOT NULL, "
    "rationale VARCHAR(300) NOT NULL, status VARCHAR(30) NOT NULL, "
    "session_id VARCHAR(100) NOT NULL, "
    "created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, PRIMARY KEY (id) )",
    "INSERT INTO chat_messages (session_id, role, content, client_ip) "
    "VALUES ('6f1c2a4e-1b2c-4d3e-8f90-123456789abc', 'user', 'old chat', '10.0.0.1')",
    "INSERT INTO email_log (lead_id, to_email, subject, status, detail) "
    "VALUES (1, 'old@example.test', 'old subject', 'sent', '')",
    "INSERT INTO ip_usage (ip, day, count, blocked, first_seen, last_seen) "
    "VALUES ('10.0.0.1', CURRENT_DATE, 3, 1, LOCALTIMESTAMP, LOCALTIMESTAMP)",
    "INSERT INTO leads (name, email, phone, budget, location, property_type, "
    "timeline_months, financing, intent, score, grade, rationale, status, session_id) "
    "VALUES ('Old', 'old@example.test', '', '80L', 'Baner', 'flat', 3, 'cash', 'buy', "
    "70, 'warm', 'kept', 'visit', '6f1c2a4e-1b2c-4d3e-8f90-123456789abc')",
]


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(user=PG_USER, host=PG_HOST, port=PG_PORT, database="postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def legacy_engine():
    assert LEGACY_DB.startswith("lead_to_lease_test"), "not a test database"
    await _admin(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')
    await _admin(f'CREATE DATABASE "{LEGACY_DB}"')
    engine = create_async_engine(
        f"postgresql+asyncpg://{PG_USER}@{PG_HOST}:{PG_PORT}/{LEGACY_DB}"
    )
    try:
        yield engine
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')


async def _snapshot(conn) -> dict:
    """Columns, constraints, indexes and rows of the old tables."""
    params = {"tables": OLD_TABLES}

    async def rows(sql):
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]

    snap = {
        "columns": await rows(
            "SELECT table_name, column_name, ordinal_position, data_type, is_nullable, "
            "       column_default, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(:tables) "
            "ORDER BY table_name, ordinal_position"
        ),
        "constraints": await rows(
            "SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid) "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "WHERE c.relname = ANY(:tables) ORDER BY 1, 2"
        ),
        "indexes": await rows(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = ANY(:tables) ORDER BY 1, 2"
        ),
    }
    for table in OLD_TABLES:
        snap[table] = await rows(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")
    return snap


async def _tables(conn) -> set[str]:
    return set(
        (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))).scalars()
    )


async def test_create_all_on_the_old_schema_adds_only_sessions_and_events(
    legacy_engine, monkeypatch
):
    async with legacy_engine.begin() as conn:
        for sql in OLD_SCHEMA:
            await conn.execute(text(sql))
    async with legacy_engine.connect() as conn:
        assert await _tables(conn) == set(OLD_TABLES)
        before = await _snapshot(conn)
    assert all(len(before[t]) == 1 for t in OLD_TABLES)

    ddl: list[str] = []

    @event.listens_for(legacy_engine.sync_engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, *args):
        words = statement.split()
        if words and words[0].upper() in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
            ddl.append(" ".join(words[:5]))

    # The real startup path, pointed at the legacy database.
    monkeypatch.setattr(bootstrap, "engine", legacy_engine)
    await bootstrap.init_models()

    assert sorted(ddl) == sorted([
        "CREATE TABLE sessions ( id",
        "CREATE INDEX sessions_ip_idx ON sessions",
        "CREATE INDEX sessions_expires_at_idx ON sessions",
        "CREATE TABLE events ( id",
        "CREATE INDEX events_session_id_ts_idx ON events",
        "CREATE INDEX events_session_type_ts_idx ON events",
    ])
    async with legacy_engine.connect() as conn:
        assert await _tables(conn) == set(OLD_TABLES) | {"sessions", "events"}
        assert await _snapshot(conn) == before

    # A second boot finds everything in place and changes nothing.
    ddl.clear()
    await bootstrap.init_models()
    assert ddl == []
    async with legacy_engine.connect() as conn:
        assert await _snapshot(conn) == before
