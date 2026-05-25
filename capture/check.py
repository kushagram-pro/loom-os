# check.py
# Quick script to verify capture and compression are working.
# Run from loom root: python check.py

import asyncio
import sys
import os
import aiosqlite

# Path setup
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'capture'))

from database import DB_PATH

async def main():
    print(f"\nDatabase: {DB_PATH}\n")

    async with aiosqlite.connect(DB_PATH) as db:

        # Raw events count
        async with db.execute('SELECT COUNT(*) FROM events') as cur:
            total = (await cur.fetchone())[0]

        # Events by source
        async with db.execute(
            'SELECT source, COUNT(*) FROM events GROUP BY source'
        ) as cur:
            by_source = await cur.fetchall()

        # Last 5 events
        async with db.execute(
            'SELECT timestamp, source, app, title FROM events ORDER BY timestamp DESC LIMIT 5'
        ) as cur:
            recent = await cur.fetchall()

        # Memory nodes count
        try:
            async with db.execute('SELECT COUNT(*) FROM memory_nodes') as cur:
                nodes = (await cur.fetchone())[0]
            async with db.execute(
                'SELECT timestamp, summary, focus_quality FROM memory_nodes ORDER BY timestamp DESC LIMIT 3'
            ) as cur:
                recent_nodes = await cur.fetchall()
        except Exception:
            nodes = 0
            recent_nodes = []

    print(f"── Raw Events ──────────────────────")
    print(f"  Total:     {total}")
    print(f"  By source:")
    for source, count in by_source:
        print(f"    {source.ljust(12)} {count}")

    print(f"\n── Last 5 Events ───────────────────")
    for ts, src, app, title in recent:
        print(f"  {ts[:16]}  [{src}]  {app.replace('.exe','')}  |  {title[:40]}")

    print(f"\n── Memory Nodes ────────────────────")
    print(f"  Total: {nodes}")
    if recent_nodes:
        for ts, summary, focus in recent_nodes:
            print(f"  {ts[:16]}  [{focus}]  {summary}")
    else:
        print("  None yet — waiting for first compression run")

    print()

asyncio.run(main())