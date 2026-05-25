# compression/debug_compression.py
#
# Run this separately to inspect what the compression engine
# has produced so far.
#
# Usage:
#   cd loom/compression
#   python debug_compression.py          → show last 5 memory nodes
#   python debug_compression.py 20       → show last 20 nodes
#   python debug_compression.py full     → show full detail on last node
#   python debug_compression.py stats    → show compression statistics
#   python debug_compression.py now      → run one compression immediately


import asyncio
import sys
import os
import json

# ── Fix Windows path resolution ───────────────────────────────────────────
_THIS_FILE   = os.path.abspath(__file__)
_COMPRESSION = os.path.dirname(_THIS_FILE)
_LOOM_ROOT   = os.path.dirname(_COMPRESSION)
_CAPTURE     = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _COMPRESSION)

from engine import (
    get_recent_memory_nodes,
    init_memory_table,
    run_compression
)
import aiosqlite
from database import DB_PATH


async def show_nodes(limit: int = 5):
    """Shows recent memory nodes in a readable format."""
    nodes = await get_recent_memory_nodes(limit)

    if not nodes:
        print("\n  No memory nodes yet.")
        print("  Run the scheduler or use: python debug_compression.py now")
        return

    print(f"\n── Last {len(nodes)} memory nodes ──\n")

    for node in nodes:
        ts       = node.get("timestamp", "")[:16].replace("T", " ")
        summary  = node.get("summary")  or "—"
        intent   = node.get("intent")   or "—"
        blockers = node.get("blockers")
        focus    = node.get("focus_quality", "—")
        stype    = node.get("session_type", "—")
        keywords = node.get("keywords", [])
        files    = node.get("files_touched", [])
        count    = node.get("raw_event_count", 0)

        print(f"  ┌─ {ts}  [{focus}] [{stype}]")
        print(f"  │  Summary:  {summary}")
        print(f"  │  Intent:   {intent}")

        if blockers and blockers not in (None, "null"):
            print(f"  │  Blockers: {blockers}")

        if keywords:
            print(f"  │  Keywords: {', '.join(keywords)}")

        if files:
            print(f"  │  Files:    {', '.join(files[:4])}")

        print(f"  └─ compressed from {count} raw events\n")


async def show_full(node: dict):
    """Shows complete detail of a single node."""
    print("\n── Full memory node detail ──\n")
    for key, val in node.items():
        if isinstance(val, list):
            print(f"  {key:20} {', '.join(val) if val else '—'}")
        else:
            print(f"  {key:20} {val or '—'}")


async def show_stats():
    """Shows compression statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT COUNT(*) FROM memory_nodes'
        ) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(
            '''SELECT focus_quality, COUNT(*) as c
               FROM memory_nodes GROUP BY focus_quality'''
        ) as cur:
            by_focus = {r[0]: r[1] for r in await cur.fetchall()}

        async with db.execute(
            '''SELECT session_type, COUNT(*) as c
               FROM memory_nodes GROUP BY session_type
               ORDER BY c DESC'''
        ) as cur:
            by_type = {r[0]: r[1] for r in await cur.fetchall()}

        async with db.execute(
            '''SELECT SUM(raw_event_count) FROM memory_nodes'''
        ) as cur:
            total_events = (await cur.fetchone())[0] or 0

        async with db.execute(
            '''SELECT timestamp FROM memory_nodes
               ORDER BY timestamp DESC LIMIT 1'''
        ) as cur:
            row = await cur.fetchone()
            latest = row[0][:16].replace("T", " ") if row else "none"

    print("\n── Compression Statistics ──\n")
    print(f"  Total memory nodes:    {total}")
    print(f"  Raw events compressed: {total_events}")
    print(f"  Latest node:           {latest}")
    print(f"\n  Focus quality breakdown:")
    for k, v in by_focus.items():
        bar = "█" * min(v, 30)
        print(f"    {k.ljust(8)} {str(v).rjust(4)}  {bar}")
    print(f"\n  Session type breakdown:")
    for k, v in by_type.items():
        bar = "█" * min(v, 30)
        print(f"    {k.ljust(14)} {str(v).rjust(4)}  {bar}")
    print()


async def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "5"

    await init_memory_table()

    if arg == "stats":
        await show_stats()

    elif arg == "full":
        nodes = await get_recent_memory_nodes(1)
        if nodes:
            await show_full(nodes[0])
        else:
            print("\n  No memory nodes yet.")

    elif arg == "now":
        print("\n[Debug] Running compression immediately...\n")
        await run_compression()

    elif arg.isdigit():
        await show_nodes(int(arg))

    else:
        await show_nodes(5)


if __name__ == "__main__":
    asyncio.run(main())