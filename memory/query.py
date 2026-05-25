# memory/query.py
#
# WHAT THIS FILE DOES:
# Three things:
#   1. Answers "what have I been working on this week?"
#   2. Detects stale projects
#   3. Finds recurring blockers
#
# This is also the entry point for the memory graph.
# Running this file starts the graph sync scheduler —
# which embeds new memory nodes every 10 minutes.
#
# HOW TO RUN:
#   cd loom
#   python memory/query.py week       → this week's work
#   python memory/query.py stale      → stale projects
#   python memory/query.py blockers   → recurring blockers
#   python memory/query.py search "JWT authentication"  → semantic search
#   python memory/query.py stats      → graph statistics
#   python memory/query.py sync       → run one sync manually
#   python memory/query.py            → start the sync scheduler
#
# THREE TERMINALS WHEN FULLY RUNNING:
#   Terminal 1: python capture/main.py
#   Terminal 2: python compression/scheduler.py
#   Terminal 3: python memory/query.py    ← this file


import asyncio
import sys
import os
import json
from datetime import datetime, timedelta

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_MEMORY    = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_MEMORY)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)

from database import DB_PATH
from graph    import sync_graph, find_similar_nodes, find_nodes_by_timerange, get_graph_stats
from projects import (
    init_projects_table, build_projects,
    get_all_projects, get_stale_projects
)
from blockers import (
    init_blockers_table, detect_recurring_blockers,
    get_recurring_blockers
)

import aiosqlite

# ── Sync interval ──────────────────────────────────────────────────────────
SYNC_INTERVAL_MINUTES = 10


# ── Query 1: What have I been working on? ─────────────────────────────────

async def query_this_week(days: int = 7):
    """
    Shows what you've been working on over the last N days.
    Groups work by project and shows session summaries.
    """
    print(f"\n── What you've worked on (last {days} days) ──────────────\n")

    # Get projects
    projects = await get_all_projects()

    if not projects:
        # Fall back to raw memory nodes if no projects yet
        nodes = find_nodes_by_timerange(days_back=days, limit=20)
        if not nodes:
            print("  No activity recorded yet.")
            print("  Make sure capture and compression services are running.")
            return

        print("  Recent sessions:\n")
        for node in nodes:
            ts      = node.get("timestamp", "")[:16].replace("T", " ")
            summary = node.get("summary") or "—"
            focus   = node.get("focus_quality", "medium")
            print(f"  {ts}  [{focus}]  {summary}")
        return

    # Show active projects first
    cutoff   = (datetime.now() - timedelta(days=days)).isoformat()
    active   = [p for p in projects if p.get("last_active", "") >= cutoff]
    inactive = [p for p in projects if p.get("last_active", "") < cutoff]

    if active:
        for project in active:
            name          = project.get("name", "Unknown")
            session_count = project.get("session_count", 0)
            last_active   = project.get("last_active", "")[:16].replace("T", " ")
            keywords      = project.get("keywords", [])[:4]
            kw_str        = ", ".join(keywords) if keywords else "—"

            # Calculate days since last active
            try:
                last_dt    = datetime.fromisoformat(project.get("last_active", ""))
                days_ago   = (datetime.now() - last_dt).days
                recency    = "today" if days_ago == 0 else f"{days_ago}d ago"
            except Exception:
                recency = "recently"

            print(f"  ▸ {name}")
            print(f"    {session_count} session(s) · last active {recency}")
            print(f"    Keywords: {kw_str}")
            print()
    else:
        print("  No active projects in this period.")

    if inactive:
        print(f"\n  Also worked on (before this period):")
        for p in inactive[:3]:
            print(f"  · {p.get('name', '?')}  ({p.get('session_count', 0)} sessions)")

    print()


# ── Query 2: Stale projects ────────────────────────────────────────────────

async def query_stale():
    """
    Shows projects with no activity in STALE_DAYS days.
    These are things you started and haven't returned to.
    """
    print(f"\n── Stale projects ──────────────────────────────────────\n")

    stale = await get_stale_projects()

    if not stale:
        print("  No stale projects detected.")
        print("  Everything you've been working on is active.")
        print()
        return

    for project in stale:
        name     = project.get("name", "Unknown")
        last_dt  = project.get("last_active", "")
        keywords = project.get("keywords", [])[:3]
        sessions = project.get("session_count", 0)

        try:
            days_ago = (
                datetime.now() - datetime.fromisoformat(last_dt)
            ).days
            stale_str = f"{days_ago} days ago"
        except Exception:
            stale_str = "a while ago"

        kw_str = ", ".join(keywords) if keywords else "—"

        print(f"  ⚠  {name}")
        print(f"     Last active: {stale_str} · {sessions} session(s)")
        print(f"     Keywords: {kw_str}")
        print()


# ── Query 3: Recurring blockers ────────────────────────────────────────────

async def query_blockers():
    """
    Shows obstacles that have appeared multiple times across sessions.
    These are patterns worth addressing deliberately.
    """
    print(f"\n── Recurring blockers ──────────────────────────────────\n")

    blockers = await get_recurring_blockers()

    if not blockers:
        print("  No recurring blockers detected yet.")
        print("  Need more sessions with blockers to detect patterns.")
        print()
        return

    for blocker in blockers:
        pattern    = blocker.get("pattern", "—")
        count      = blocker.get("occurrences", 0)
        first_seen = blocker.get("first_seen", "")[:10]
        last_seen  = blocker.get("last_seen", "")[:10]
        examples   = blocker.get("examples", [])

        print(f"  ✦ {pattern}")
        print(f"    Appeared {count} times · {first_seen} → {last_seen}")

        if len(examples) > 1:
            # Show one alternate phrasing
            alt = [e for e in examples if e != pattern]
            if alt:
                print(f"    Also: \"{alt[0][:80]}\"")
        print()


# ── Semantic search ────────────────────────────────────────────────────────

async def query_search(search_text: str):
    """
    Finds memory nodes semantically related to a query.
    """
    print(f"\n── Search: \"{search_text}\" ─────────────────────────────\n")

    results = find_similar_nodes(search_text, limit=5, min_score=0.5)

    if not results:
        print("  No similar sessions found.")
        print("  Try a different query or wait for more sessions to accumulate.")
        print()
        return

    for result in results:
        ts      = result.get("timestamp", "")[:16].replace("T", " ")
        summary = result.get("summary") or "—"
        score   = result.get("_score", 0)
        focus   = result.get("focus_quality", "—")
        files   = result.get("files_touched", [])[:2]

        print(f"  {ts}  [{focus}]  similarity: {score}")
        print(f"  {summary}")
        if files:
            print(f"  Files: {', '.join(files)}")
        print()


# ── Graph stats ────────────────────────────────────────────────────────────

async def query_stats():
    """
    Shows the current state of the memory graph.
    """
    print(f"\n── Memory graph statistics ─────────────────────────────\n")

    stats    = await get_graph_stats()
    projects = await get_all_projects()
    blockers = await get_recurring_blockers()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM events') as cur:
            raw_events = (await cur.fetchone())[0]

    print(f"  Raw events captured:    {raw_events}")
    print(f"  Memory nodes created:   {stats['total_memory_nodes']}")
    print(f"  Nodes embedded:         {stats['total_embedded']}")
    print(f"  Nodes pending embed:    {stats['unembedded']}")
    print(f"  Projects detected:      {len(projects)}")
    print(f"  Recurring blockers:     {len(blockers)}")
    print()


# ── Full sync ──────────────────────────────────────────────────────────────

async def run_full_sync():
    """
    Runs a complete memory graph sync:
    1. Embed any new memory nodes
    2. Rebuild project clusters
    3. Detect recurring blockers
    """
    print(f"\n[Memory] Full sync at {datetime.now().strftime('%H:%M:%S')}")
    print("─" * 48)

    # Step 1: Embed new nodes
    embedded = await sync_graph()

    # Step 2: Rebuild projects
    if embedded > 0:
        await build_projects()

    # Step 3: Detect blockers
    await detect_recurring_blockers()

    print("[Memory] Sync complete")


# ── Scheduler ─────────────────────────────────────────────────────────────

async def run_scheduler():
    """
    Runs the full sync every SYNC_INTERVAL_MINUTES.
    This keeps the memory graph current as new compression
    nodes are created by the compression scheduler.
    """
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║       L · O · O · M                         ║")
    print("  ║       Memory Graph                           ║")
    print(f"  ║       Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}          ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    print(f"  Sync interval: every {SYNC_INTERVAL_MINUTES} minutes")
    print(f"  Vector store:  loom_vectors/")
    print(f"  Stop:          Ctrl+C")
    print()

    # Init all tables
    await init_projects_table()
    await init_blockers_table()

    run = 0
    while True:
        run += 1
        try:
            await run_full_sync()
        except Exception as e:
            print(f"[Memory] Sync #{run} failed: {e}")

        next_run = (
            datetime.now() + timedelta(minutes=SYNC_INTERVAL_MINUTES)
        ).strftime("%H:%M:%S")
        print(f"[Memory] Next sync at {next_run}\n")
        await asyncio.sleep(SYNC_INTERVAL_MINUTES * 60)


# ── Entry point ────────────────────────────────────────────────────────────

async def main():
    # Init tables first
    await init_projects_table()
    await init_blockers_table()

    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "week":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        await query_this_week(days)

    elif arg == "stale":
        await query_stale()

    elif arg == "blockers":
        await query_blockers()

    elif arg == "search":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text:
            print("Usage: python query.py search <your query text>")
        else:
            await query_search(query_text)

    elif arg == "stats":
        await query_stats()

    elif arg == "sync":
        await run_full_sync()

    else:
        # No argument = start scheduler
        await run_scheduler()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[Memory] Graph scheduler stopped.")
        print()