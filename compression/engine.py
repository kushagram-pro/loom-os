# compression/engine.py
#
# WHAT THIS FILE DOES:
# The core compression logic. One job:
# Take raw events from SQLite → send to Phi-4 mini → save memory node.
#
# CALLED BY:
# scheduler.py — which runs this every 30 minutes automatically.
# You can also call run_compression() manually for testing.
#
# TWO TABLES IN ONE DATABASE:
# events        → raw captured activity (written by capture service)
# memory_nodes  → compressed semantic memory (written by this engine)
# Both live in loom_events.db. One file. Everything local.
#
# THE WINDOWING LOGIC:
# Each compression run covers events since the last run ended.
# This means no event is ever compressed twice.
# If compression fails, the next run picks up from where it left off.
#
# HOW TO TEST MANUALLY:
#   cd loom
#   python -c "
#   import asyncio, sys
#   sys.path.insert(0, 'compression')
#   sys.path.insert(0, 'capture')
#   from engine import run_compression, init_memory_table
#   async def test():
#       await init_memory_table()
#       await run_compression()
#   asyncio.run(test())
#   "

import asyncio
import json
import sys
import os
from datetime import datetime, timedelta

import aiosqlite
import ollama

# ── Fix Windows path resolution ───────────────────────────────────────────
# Get the absolute path to the loom root no matter where you run from
_THIS_FILE   = os.path.abspath(__file__)           # .../loom/compression/engine.py
_COMPRESSION = os.path.dirname(_THIS_FILE)         # .../loom/compression
_LOOM_ROOT   = os.path.dirname(_COMPRESSION)       # .../loom
_CAPTURE     = os.path.join(_LOOM_ROOT, 'capture') # .../loom/capture

# Insert at position 0 so our paths take priority
sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _COMPRESSION)

from database import DB_PATH
from prompt import build_compression_prompt, parse_compression_response

# ── Configuration ──────────────────────────────────────────────────────────
MODEL_NAME         = "phi4-mini"  # Ollama model name — must match ollama list
MIN_EVENTS         = 5            # Skip compression if fewer events than this
MAX_EVENTS         = 150          # Cap to avoid overwhelming the model context
WINDOW_MINUTES     = 35           # Slightly more than 30 to avoid edge gaps
OLLAMA_TEMPERATURE = 0.1          # Low = consistent and precise output
OLLAMA_MAX_TOKENS  = 500          # Enough for our JSON structure


# ── Database setup ─────────────────────────────────────────────────────────

async def init_memory_table():
    """
    Creates the memory_nodes table in loom_events.db.
    Safe to call on every startup — won't overwrite existing data.

    Column meanings:
    ┌─────────────────┬──────────────────────────────────────────────────┐
    │ id              │ Auto-incremented unique ID                       │
    │ timestamp       │ When this node was created                       │
    │ period_start    │ Start of the raw event window this covers        │
    │ period_end      │ End of the raw event window this covers          │
    │ summary         │ What was worked on — specific and named          │
    │ intent          │ What the developer was trying to accomplish       │
    │ blockers        │ What got in the way (null if none)               │
    │ apps_used       │ JSON array: app names used in this session       │
    │ files_touched   │ JSON array: filenames edited in this session     │
    │ focus_quality   │ high / medium / low                              │
    │ session_type    │ coding / debugging / research / etc.             │
    │ keywords        │ JSON array: specific technical keywords          │
    │ raw_event_count │ How many raw events were compressed              │
    └─────────────────┴──────────────────────────────────────────────────┘
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS memory_nodes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT    NOT NULL,
                period_start     TEXT    NOT NULL,
                period_end       TEXT    NOT NULL,
                summary          TEXT,
                intent           TEXT,
                blockers         TEXT,
                apps_used        TEXT    DEFAULT "[]",
                files_touched    TEXT    DEFAULT "[]",
                focus_quality    TEXT    DEFAULT "medium",
                session_type     TEXT    DEFAULT "mixed",
                keywords         TEXT    DEFAULT "[]",
                raw_event_count  INTEGER DEFAULT 0
            )
        ''')
        # Index for fast time-range queries — used by memory graph layer
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_node_timestamp
            ON memory_nodes (timestamp DESC)
        ''')
        await db.commit()

    print("[Engine] Memory nodes table ready")


# ── Event fetching ─────────────────────────────────────────────────────────

async def get_uncompressed_events() -> tuple[list[dict], str, str]:
    """
    Fetches raw events that haven't been compressed yet.

    Smart windowing:
    - If memory_nodes table has entries → start from last period_end
    - If no entries yet (first run) → go back WINDOW_MINUTES
    This ensures no event is ever compressed twice.

    Returns: (events list, period_start string, period_end string)
    """
    now        = datetime.now()
    period_end = now.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:

        # Find where the last compression run ended
        async with db.execute(
            'SELECT period_end FROM memory_nodes ORDER BY timestamp DESC LIMIT 1'
        ) as cur:
            row = await cur.fetchone()

        if row:
            # Continue from where we left off
            period_start = row[0]
        else:
            # First ever run — go back a full window
            period_start = (
                now - timedelta(minutes=WINDOW_MINUTES)
            ).isoformat()

        # Fetch events in this window, ordered chronologically
        db.row_factory = aiosqlite.Row
        async with db.execute(
            '''SELECT * FROM events
               WHERE timestamp > ? AND timestamp <= ?
               ORDER BY timestamp ASC
               LIMIT ?''',
            (period_start, period_end, MAX_EVENTS)
        ) as cur:
            rows = await cur.fetchall()
            events = [dict(r) for r in rows]

    return events, period_start, period_end


# ── Memory node saving ─────────────────────────────────────────────────────

async def save_memory_node(node: dict):
    """
    Saves a compressed memory node to the memory_nodes table.
    List fields are JSON-serialised for storage.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO memory_nodes (
                timestamp, period_start, period_end,
                summary, intent, blockers,
                apps_used, files_touched,
                focus_quality, session_type,
                keywords, raw_event_count
            ) VALUES (
                :timestamp, :period_start, :period_end,
                :summary, :intent, :blockers,
                :apps_used, :files_touched,
                :focus_quality, :session_type,
                :keywords, :raw_event_count
            )
        ''', {
            "timestamp":       node["timestamp"],
            "period_start":    node["period_start"],
            "period_end":      node["period_end"],
            "summary":         node.get("summary"),
            "intent":          node.get("intent"),
            "blockers":        node.get("blockers"),
            "apps_used":       json.dumps(node.get("apps_used", [])),
            "files_touched":   json.dumps(node.get("files_touched", [])),
            "focus_quality":   node.get("focus_quality", "medium"),
            "session_type":    node.get("session_type", "mixed"),
            "keywords":        json.dumps(node.get("keywords", [])),
            "raw_event_count": node.get("raw_event_count", 0),
        })
        await db.commit()


# ── Recent nodes query ─────────────────────────────────────────────────────

async def get_recent_memory_nodes(limit: int = 10) -> list[dict]:
    """
    Returns the most recent memory nodes.
    Used by debug.py and eventually by the surface layer.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            '''SELECT * FROM memory_nodes
               ORDER BY timestamp DESC LIMIT ?''',
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            nodes = []
            for r in rows:
                node = dict(r)
                # Deserialise JSON array fields
                for field in ["apps_used", "files_touched", "keywords"]:
                    try:
                        node[field] = json.loads(node.get(field) or "[]")
                    except Exception:
                        node[field] = []
                nodes.append(node)
            return nodes


# ── Core compression run ───────────────────────────────────────────────────

async def run_compression():
    """
    Single compression run. Called every 30 minutes by scheduler.py.
    Can also be called manually for testing.

    Flow:
    1.  Fetch uncompressed raw events
    2.  Check event count threshold
    3.  Build prompt from events
    4.  Call Phi-4 mini via Ollama
    5.  Parse the JSON response
    6.  Save memory node to database
    7.  Print result to terminal

    Every step has error handling — a single bad run never
    crashes the scheduler or loses any data.
    """
    now = datetime.now()
    print(f"\n[Engine] Compression run at {now.strftime('%H:%M:%S')}")
    print(f"{'─' * 48}")

    # ── Step 1: Fetch events ──────────────────────────────────────────────
    try:
        events, period_start, period_end = await get_uncompressed_events()
    except Exception as e:
        print(f"[Engine] ✗ Failed to fetch events: {e}")
        return

    event_count = len(events)
    print(f"[Engine] Events in window: {event_count}")

    # ── Step 2: Threshold check ───────────────────────────────────────────
    if event_count < MIN_EVENTS:
        print(f"[Engine] Below minimum threshold ({MIN_EVENTS}). Skipping.")
        return

    # ── Step 3: Build prompt ──────────────────────────────────────────────
    prompt = build_compression_prompt(events)

    if not prompt:
        print("[Engine] No meaningful events to compress. Skipping.")
        return

    # Show a preview of what's being compressed
    lines = prompt.split('\n')
    activity_lines = [l for l in lines if l.startswith('- ')]
    print(f"[Engine] Compressing {len(activity_lines)} activity lines...")

    # ── Step 4: Call Phi-4 mini ───────────────────────────────────────────
    try:
        call_start = datetime.now()
        print(f"[Engine] Calling {MODEL_NAME}...")

        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            options={
                # Low temperature = deterministic, structured output
                # High temperature = creative but inconsistent
                "temperature":  OLLAMA_TEMPERATURE,
                "num_predict":  OLLAMA_MAX_TOKENS,
            }
        )

        elapsed = (datetime.now() - call_start).total_seconds()
        response_text = response['message']['content']
        print(f"[Engine] Model responded in {round(elapsed, 1)}s")

    except ollama.ResponseError as e:
        print(f"[Engine] ✗ Ollama error: {e}")
        print(f"[Engine]   Is the model pulled? Run: ollama pull {MODEL_NAME}")
        return
    except Exception as e:
        print(f"[Engine] ✗ Failed to call Ollama: {e}")
        print(f"[Engine]   Is Ollama running? Run: ollama serve")
        return

    # ── Step 5: Parse response ────────────────────────────────────────────
    parsed = parse_compression_response(response_text)

    if not parsed:
        print(f"[Engine] ✗ Could not parse model response.")
        print(f"[Engine]   Raw response (first 300 chars):")
        print(f"           {response_text[:300]}")
        return

    # ── Step 6: Save memory node ──────────────────────────────────────────
    node = {
        "timestamp":       now.isoformat(),
        "period_start":    period_start,
        "period_end":      period_end,
        "raw_event_count": event_count,
        **parsed
    }

    try:
        await save_memory_node(node)
    except Exception as e:
        print(f"[Engine] ✗ Failed to save memory node: {e}")
        return

    # ── Step 7: Print result ──────────────────────────────────────────────
    print(f"[Engine] ✓ Memory node saved")
    print(f"")

    summary  = parsed.get("summary")  or "—"
    intent   = parsed.get("intent")   or "—"
    blockers = parsed.get("blockers")
    focus    = parsed.get("focus_quality", "—")
    stype    = parsed.get("session_type", "—")
    keywords = parsed.get("keywords", [])

    print(f"  Summary:  {summary}")
    print(f"  Intent:   {intent}")

    if blockers and blockers not in (None, "null", "NULL"):
        print(f"  Blockers: {blockers}")
    else:
        print(f"  Blockers: none detected")

    print(f"  Focus:    {focus}  |  Type: {stype}")

    if keywords:
        print(f"  Keywords: {', '.join(keywords)}")

    files = parsed.get("files_touched", [])
    if files:
        print(f"  Files:    {', '.join(files[:5])}")

    print(f"  Events compressed: {event_count}")
    print(f"{'─' * 48}")