# database.py
# Async SQLite storage for all captured events.
# Uses aiosqlite so writes never block the event loop.

import aiosqlite
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'loom.db')


async def init_db():
    """Create the events table if it doesn't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT    NOT NULL,
                source     TEXT    NOT NULL,
                app        TEXT,
                title      TEXT,
                detail     TEXT,
                duration   REAL    DEFAULT 0,
                importance TEXT,
                extra      TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)"
        )
        await db.commit()


async def save_event(event: dict):
    """
    Persist a single event dict to the database.
    Known fields get their own columns; anything extra goes into JSON.
    """
    known = {"timestamp", "source", "app", "title", "detail", "duration", "importance"}
    extra = {k: v for k, v in event.items() if k not in known}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO events
                (timestamp, source, app, title, detail, duration, importance, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("timestamp", datetime.now().isoformat()),
                event.get("source", ""),
                event.get("app", ""),
                event.get("title", ""),
                event.get("detail", ""),
                event.get("duration", 0),
                event.get("importance", "normal"),
                json.dumps(extra) if extra else None,
            )
        )
        await db.commit()


async def query_events(
    since: str | None = None,
    source: str | None = None,
    limit: int = 100
) -> list[dict]:
    """Fetch recent events with optional filters."""
    conditions = []
    params: list = []

    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    if source:
        conditions.append("source = ?")
        params.append(source)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?",
            params
        ) as cursor:
            rows = await cursor.fetchall()

    results = []
    for row in rows:
        record = dict(row)
        if record.get("extra"):
            record.update(json.loads(record.pop("extra")))
        else:
            record.pop("extra", None)
        results.append(record)

    return results
