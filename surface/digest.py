# surface/digest.py
#
# Updated with richer data:
# - Focus quality and deep work duration today
# - Memory stats (events, nodes, next compression)
# - Work streak (consecutive days of activity)
# - Last file touched
# - Active project with session count


import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

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
    # Greeting
    greeting:           str = "Good evening"
    user_name:          str = ""

    # Last session
    last_worked_on:     str = "Nothing recorded yet"
    last_file:          str = ""
    last_session_ago:   str = ""

    # Focus
    focus_quality:      str = "medium"
    focus_minutes_today: int = 0
    focus_bar:          str = ""        # visual bar e.g. "████████░░"

    # Project
    current_project:    str = ""
    project_sessions:   int = 0

    # Streak
    streak_days:        int = 0

    # Memory stats
    events_captured:    int = 0
    memory_nodes:       int = 0
    projects_detected:  int = 0
    mins_to_next_sync:  int = 0

    # Alerts
    stale_projects:     list = field(default_factory=list)
    recurring_blocker:  str = ""

    # Suggestion
    focus_today:        str = "Keep building"

    generated_at:       str = ""


def time_ago(iso_timestamp: str) -> str:
    if not iso_timestamp:
        return "a while ago"
    try:
        delta = datetime.now() - datetime.fromisoformat(iso_timestamp)
        secs  = delta.total_seconds()
        if secs < 60:       return "just now"
        elif secs < 3600:   return f"{int(secs/60)}m ago"
        elif secs < 86400:  return f"{int(secs/3600)}h ago"
        elif secs < 172800: return "yesterday"
        else:               return f"{int(secs/86400)}d ago"
    except Exception:
        return "recently"


def make_focus_bar(minutes: int, max_minutes: int = 240) -> str:
    """
    Generates a 10-block progress bar for focus time.
    max_minutes = 4 hours = full bar
    e.g. 2 hours = 5 filled blocks → "█████░░░░░"
    """
    filled = min(10, int((minutes / max_minutes) * 10))
    return "█" * filled + "░" * (10 - filled)


def get_greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:   return "Good morning"
    elif hour < 17: return "Good afternoon"
    else:           return "Good evening"


async def generate_digest() -> DigestData:
    d = DigestData(
        greeting=get_greeting(),
        generated_at=datetime.now().isoformat()
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── Last memory node ──────────────────────────────────────────────
        async with db.execute(
            'SELECT * FROM memory_nodes ORDER BY timestamp DESC LIMIT 1'
        ) as cur:
            row = await cur.fetchone()

        if row:
            node = dict(row)
            d.last_worked_on  = node.get("summary") or "Recent session"
            d.last_session_ago = time_ago(node.get("timestamp", ""))
            d.focus_quality    = node.get("focus_quality") or "medium"

            files = node.get("files_touched", "[]")
            try:
                files = json.loads(files)
            except Exception:
                files = []
            if files:
                d.last_file = files[0].replace("\\", "/").split("/")[-1]

        # ── Focus time today ──────────────────────────────────────────────
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        async with db.execute(
            '''SELECT SUM(duration) FROM events
               WHERE source = "rhythm"
               AND title = "focus_burst"
               AND timestamp >= ?''',
            (today_start,)
        ) as cur:
            result = await cur.fetchone()
            total_focus_secs  = result[0] or 0
            d.focus_minutes_today = int(total_focus_secs / 60)
            d.focus_bar = make_focus_bar(d.focus_minutes_today)

        # ── Raw event count ───────────────────────────────────────────────
        async with db.execute('SELECT COUNT(*) FROM events') as cur:
            d.events_captured = (await cur.fetchone())[0]

        # ── Memory node count ─────────────────────────────────────────────
        async with db.execute('SELECT COUNT(*) FROM memory_nodes') as cur:
            d.memory_nodes = (await cur.fetchone())[0]

        # ── Next compression countdown ────────────────────────────────────
        async with db.execute(
            'SELECT timestamp FROM memory_nodes ORDER BY timestamp DESC LIMIT 1'
        ) as cur:
            row = await cur.fetchone()

        if row:
            last_node_time = datetime.fromisoformat(row[0])
            elapsed_mins   = (datetime.now() - last_node_time).total_seconds() / 60
            d.mins_to_next_sync = max(0, int(30 - elapsed_mins))
        else:
            d.mins_to_next_sync = 30

        # ── Projects ──────────────────────────────────────────────────────
        try:
            async with db.execute(
                'SELECT COUNT(*) FROM projects'
            ) as cur:
                d.projects_detected = (await cur.fetchone())[0]

            async with db.execute(
                '''SELECT name, session_count FROM projects
                   WHERE is_stale = 0
                   ORDER BY last_active DESC LIMIT 1'''
            ) as cur:
                row = await cur.fetchone()
            if row:
                d.current_project  = row[0]
                d.project_sessions = row[1]
        except Exception:
            pass

        # ── Streak — consecutive days with activity ───────────────────────
        try:
            streak = 0
            check_date = datetime.now().date()
            for _ in range(30):  # check up to 30 days back
                day_start = datetime.combine(check_date, datetime.min.time()).isoformat()
                day_end   = datetime.combine(check_date + timedelta(days=1), datetime.min.time()).isoformat()
                async with db.execute(
                    'SELECT COUNT(*) FROM memory_nodes WHERE timestamp >= ? AND timestamp < ?',
                    (day_start, day_end)
                ) as cur:
                    count = (await cur.fetchone())[0]
                if count > 0:
                    streak += 1
                    check_date -= timedelta(days=1)
                else:
                    break
            d.streak_days = streak
        except Exception:
            d.streak_days = 0

        # ── Stale projects ────────────────────────────────────────────────
        try:
            async with db.execute(
                '''SELECT name, last_active FROM projects
                   WHERE is_stale = 1
                   ORDER BY last_active ASC LIMIT 2'''
            ) as cur:
                rows = await cur.fetchall()
            d.stale_projects = [
                {"name": r[0], "ago": time_ago(r[1])}
                for r in rows
            ]
        except Exception:
            d.stale_projects = []

        # ── Recurring blocker ─────────────────────────────────────────────
        try:
            async with db.execute(
                'SELECT pattern, occurrences FROM recurring_blockers ORDER BY occurrences DESC LIMIT 1'
            ) as cur:
                row = await cur.fetchone()
            if row:
                d.recurring_blocker = f"{row[0][:55]} (×{row[1]})"
        except Exception:
            pass

        # ── Focus suggestion ──────────────────────────────────────────────
        if d.streak_days >= 3:
            d.focus_today = f"Day {d.streak_days} streak — keep it going"
        elif d.stale_projects:
            name = d.stale_projects[0]["name"]
            ago  = d.stale_projects[0]["ago"]
            d.focus_today = f"Consider returning to {name} — {ago}"
        elif d.current_project:
            d.focus_today = f"Continue work on {d.current_project}"
        else:
            d.focus_today = "Keep building"

    return d