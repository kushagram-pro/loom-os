# surface/digest.py
#
# WHAT THIS FILE DOES:
# Generates the morning digest — a concise summary of what
# you were working on, where you left off, and what deserves
# attention today.
#
# WHEN IT RUNS:
# Once per day, on first launch of Loom Bar.
# Cached in SQLite so it doesn't regenerate on every restart.
#
# WHAT IT READS:
# - Last 3 memory nodes     → what you were doing recently
# - Most active project     → your current main focus
# - Stale projects          → things slipping away
# - Recurring blockers      → patterns worth addressing
#
# OUTPUT:
# A DigestData dataclass with clean strings ready for display.
# The bar.py UI reads this directly.


import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_SURFACE   = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_SURFACE)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')
_MEMORY    = os.path.join(_LOOM_ROOT, 'memory')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)

from database import DB_PATH


@dataclass
class DigestData:
    """
    Clean structured data for the morning digest display.
    All fields are strings ready to show in the UI.
    """
    greeting:          str = "Good morning"
    last_worked_on:    str = "Nothing recorded yet"
    last_file:         str = ""
    last_session_ago:  str = ""
    current_project:   str = ""
    stale_projects:    list = None
    recurring_blocker: str = ""
    focus_today:       str = ""
    sessions_today:    int = 0
    generated_at:      str = ""

    def __post_init__(self):
        if self.stale_projects is None:
            self.stale_projects = []


def time_ago(iso_timestamp: str) -> str:
    """
    Converts an ISO timestamp to a human-readable "time ago" string.
    Examples: "2 hours ago", "yesterday", "3 days ago"
    """
    if not iso_timestamp:
        return "a while ago"

    try:
        then  = datetime.fromisoformat(iso_timestamp)
        delta = datetime.now() - then
        secs  = delta.total_seconds()

        if secs < 60:
            return "just now"
        elif secs < 3600:
            mins = int(secs / 60)
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        elif secs < 86400:
            hours = int(secs / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif secs < 172800:
            return "yesterday"
        else:
            days = int(secs / 86400)
            return f"{days} days ago"
    except Exception:
        return "recently"


def get_greeting() -> str:
    """Returns a time-appropriate greeting."""
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"


async def generate_digest() -> DigestData:
    """
    Reads the memory graph and generates a DigestData object.
    This is the main function called by the surface layer.
    """
    digest = DigestData(
        greeting=get_greeting(),
        generated_at=datetime.now().isoformat()
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── Last memory node ──────────────────────────────────────────────
        async with db.execute(
            '''SELECT * FROM memory_nodes
               ORDER BY timestamp DESC LIMIT 1'''
        ) as cur:
            row = await cur.fetchone()

        if row:
            node = dict(row)
            digest.last_worked_on  = node.get("summary") or "Recent work session"
            digest.last_session_ago = time_ago(node.get("timestamp", ""))

            # Most recently touched file
            files = node.get("files_touched", "[]")
            try:
                files = json.loads(files)
            except Exception:
                files = []
            if files:
                digest.last_file = files[0].replace("\\", "/").split("/")[-1]

        # ── Sessions today ────────────────────────────────────────────────
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        async with db.execute(
            'SELECT COUNT(*) FROM memory_nodes WHERE timestamp >= ?',
            (today_start,)
        ) as cur:
            digest.sessions_today = (await cur.fetchone())[0]

        # ── Most active project ───────────────────────────────────────────
        try:
            async with db.execute(
                '''SELECT name FROM projects
                   WHERE is_stale = 0
                   ORDER BY last_active DESC LIMIT 1'''
            ) as cur:
                row = await cur.fetchone()
            if row:
                digest.current_project = row[0]
        except Exception:
            pass  # Projects table may not exist yet

        # ── Stale projects ────────────────────────────────────────────────
        try:
            async with db.execute(
                '''SELECT name, last_active FROM projects
                   WHERE is_stale = 1
                   ORDER BY last_active ASC LIMIT 3'''
            ) as cur:
                rows = await cur.fetchall()
            digest.stale_projects = [
                {
                    "name": r[0],
                    "ago":  time_ago(r[1])
                }
                for r in rows
            ]
        except Exception:
            digest.stale_projects = []

        # ── Recurring blocker ─────────────────────────────────────────────
        try:
            async with db.execute(
                '''SELECT pattern, occurrences FROM recurring_blockers
                   ORDER BY occurrences DESC LIMIT 1'''
            ) as cur:
                row = await cur.fetchone()
            if row:
                digest.recurring_blocker = (
                    f"{row[0][:60]} (×{row[1]})"
                )
        except Exception:
            pass

        # ── Focus suggestion ──────────────────────────────────────────────
        # Simple rule: if stale projects exist → suggest returning
        # Otherwise → suggest continuing current project
        if digest.stale_projects:
            stale_name = digest.stale_projects[0]["name"]
            stale_ago  = digest.stale_projects[0]["ago"]
            digest.focus_today = f"Consider returning to {stale_name} — last touched {stale_ago}"
        elif digest.current_project:
            digest.focus_today = f"Continue work on {digest.current_project}"
        else:
            digest.focus_today = "Keep building"

    return digest