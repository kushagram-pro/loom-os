# surface/alerts.py
#
# WHAT THIS FILE DOES:
# Generates alerts for stale projects and recurring blockers.
# Learns from user behavior — surfaces more of what you engage with,
# less of what you ignore.
#
# THE BEHAVIOR LEARNING LOOP:
# Every alert has an engagement score tracked in SQLite.
# When user engages (clicks, hovers, expands) → score goes up
# When user dismisses immediately → score goes down
# Score determines how frequently that alert type appears
#
# ALERT TYPES:
# - stale_project    → project with no activity in N days
# - recurring_blocker → same obstacle appearing 3+ times
# - context_restore   → returning to a project after a break
# - focus_nudge      → you've been idle, here's what you were doing
#
# STORAGE:
# alerts_log table in SQLite tracks every alert shown and
# whether the user engaged with it.


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


# ── Alert data structure ───────────────────────────────────────────────────

@dataclass
class Alert:
    """Represents a single alert to be shown in the Loom Bar."""
    alert_type: str       # stale_project / recurring_blocker / context_restore / focus_nudge
    title:      str       # Short headline — shown in collapsed bar
    body:       str       # Full message — shown when expanded
    priority:   int = 1   # 1=low, 2=medium, 3=high
    data:       dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}


# ── Database setup ─────────────────────────────────────────────────────────

async def init_alerts_tables():
    """
    Creates alerts_log and alert_preferences tables.
    """
    async with aiosqlite.connect(DB_PATH) as db:

        # Log of every alert shown
        await db.execute('''
            CREATE TABLE IF NOT EXISTS alerts_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                alert_type   TEXT    NOT NULL,
                title        TEXT,
                engaged      INTEGER DEFAULT 0,
                dismissed    INTEGER DEFAULT 0,
                visible_secs REAL    DEFAULT 0
            )
        ''')

        # Learned preferences per alert type
        # engagement_score: higher = show more, lower = show less
        await db.execute('''
            CREATE TABLE IF NOT EXISTS alert_preferences (
                alert_type       TEXT PRIMARY KEY,
                engagement_score REAL DEFAULT 1.0,
                total_shown      INTEGER DEFAULT 0,
                total_engaged    INTEGER DEFAULT 0,
                frequency_mins   INTEGER DEFAULT 30
            )
        ''')

        # Seed default preferences if not exists
        for alert_type, default_freq in [
            ("stale_project",     60),
            ("recurring_blocker", 120),
            ("context_restore",   0),   # always show
            ("focus_nudge",       45),
        ]:
            await db.execute('''
                INSERT OR IGNORE INTO alert_preferences
                    (alert_type, frequency_mins)
                VALUES (?, ?)
            ''', (alert_type, default_freq))

        await db.commit()


# ── Behavior tracking ──────────────────────────────────────────────────────

async def log_alert_shown(alert_type: str, title: str) -> int:
    """
    Logs that an alert was shown. Returns the log entry ID
    so we can update it later when user engages or dismisses.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO alerts_log (timestamp, alert_type, title)
            VALUES (?, ?, ?)
        ''', (datetime.now().isoformat(), alert_type, title))
        await db.commit()
        return cursor.lastrowid


async def log_alert_engagement(
    log_id: int,
    engaged: bool,
    visible_secs: float
):
    """
    Updates an alert log entry with user engagement data.
    Then recalculates the engagement score for that alert type.

    Called by bar.py when user interacts with or dismisses an alert.
    """
    async with aiosqlite.connect(DB_PATH) as db:

        # Update this specific alert log
        await db.execute('''
            UPDATE alerts_log
            SET engaged      = ?,
                dismissed    = ?,
                visible_secs = ?
            WHERE id = ?
        ''', (
            1 if engaged else 0,
            0 if engaged else 1,
            visible_secs,
            log_id
        ))

        # Recalculate engagement score for this alert type
        async with db.execute(
            'SELECT alert_type FROM alerts_log WHERE id = ?', (log_id,)
        ) as cur:
            row = await cur.fetchone()

        if row:
            alert_type = row[0]

            # Get engagement stats for this type
            async with db.execute('''
                SELECT
                    COUNT(*) as total,
                    SUM(engaged) as engaged_count
                FROM alerts_log
                WHERE alert_type = ?
            ''', (alert_type,)) as cur:
                stats = await cur.fetchone()

            total         = stats[0] or 1
            engaged_count = stats[1] or 0
            score         = engaged_count / total

            # Adjust frequency based on score
            # High engagement (>0.6) → show more often (lower freq)
            # Low engagement (<0.2)  → show less often (higher freq)
            if score > 0.6:
                freq_adjustment = 0.8    # 20% more frequent
            elif score < 0.2:
                freq_adjustment = 1.5    # 50% less frequent
            else:
                freq_adjustment = 1.0    # no change

            async with db.execute(
                'SELECT frequency_mins FROM alert_preferences WHERE alert_type = ?',
                (alert_type,)
            ) as cur:
                pref_row = await cur.fetchone()

            if pref_row:
                base_freq    = pref_row[0]
                new_freq     = max(15, min(240, int(base_freq * freq_adjustment)))

                await db.execute('''
                    UPDATE alert_preferences
                    SET engagement_score = ?,
                        total_shown      = ?,
                        total_engaged    = ?,
                        frequency_mins   = ?
                    WHERE alert_type = ?
                ''', (score, total, engaged_count, new_freq, alert_type))

        await db.commit()


async def should_show_alert(alert_type: str) -> bool:
    """
    Checks if enough time has passed since the last alert
    of this type was shown, based on learned frequency.
    Returns True if the alert should be shown now.
    """
    if alert_type == "context_restore":
        return True  # Always show context restores

    async with aiosqlite.connect(DB_PATH) as db:

        # Get learned frequency for this type
        async with db.execute(
            'SELECT frequency_mins FROM alert_preferences WHERE alert_type = ?',
            (alert_type,)
        ) as cur:
            row = await cur.fetchone()
        freq_mins = row[0] if row else 30

        # Check when this type was last shown
        async with db.execute('''
            SELECT timestamp FROM alerts_log
            WHERE alert_type = ?
            ORDER BY timestamp DESC LIMIT 1
        ''', (alert_type,)) as cur:
            row = await cur.fetchone()

        if not row:
            return True  # Never shown → show it

        last_shown = datetime.fromisoformat(row[0])
        elapsed    = (datetime.now() - last_shown).total_seconds() / 60

        return elapsed >= freq_mins


# ── Alert generation ───────────────────────────────────────────────────────

async def get_pending_alerts() -> list[Alert]:
    """
    Checks all alert conditions and returns alerts that
    should be shown right now based on:
    1. Alert condition is true (stale project exists, etc.)
    2. Enough time has passed since last shown (learned frequency)
    3. Priority ordering — highest priority first

    Returns a list of Alert objects ready for display.
    """
    alerts = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── Stale project alerts ──────────────────────────────────────────
        if await should_show_alert("stale_project"):
            try:
                async with db.execute(
                    '''SELECT name, last_active, session_count
                       FROM projects WHERE is_stale = 1
                       ORDER BY last_active ASC LIMIT 2'''
                ) as cur:
                    rows = await cur.fetchall()

                for row in rows:
                    name     = row["name"]
                    last_dt  = datetime.fromisoformat(row["last_active"])
                    days_ago = (datetime.now() - last_dt).days

                    alerts.append(Alert(
                        alert_type = "stale_project",
                        title      = f"⚠ {name} — {days_ago} days inactive",
                        body       = (
                            f"You haven't worked on '{name}' in {days_ago} days. "
                            f"It had {row['session_count']} session(s) of activity."
                        ),
                        priority   = 2 if days_ago > 7 else 1,
                        data       = {"project_name": name, "days_ago": days_ago}
                    ))
            except Exception:
                pass

        # ── Recurring blocker alerts ──────────────────────────────────────
        if await should_show_alert("recurring_blocker"):
            try:
                async with db.execute(
                    '''SELECT pattern, occurrences, last_seen
                       FROM recurring_blockers
                       ORDER BY occurrences DESC LIMIT 1'''
                ) as cur:
                    row = await cur.fetchone()

                if row:
                    alerts.append(Alert(
                        alert_type = "recurring_blocker",
                        title      = f"✦ Recurring blocker detected",
                        body       = (
                            f"This obstacle has appeared {row['occurrences']} times: "
                            f"\"{row['pattern'][:80]}\""
                        ),
                        priority   = 2,
                        data       = {"pattern": row["pattern"], "count": row["occurrences"]}
                    ))
            except Exception:
                pass

        # ── Focus nudge — after idle ──────────────────────────────────────
        if await should_show_alert("focus_nudge"):
            try:
                # Check if there was a recent idle_end event
                async with db.execute(
                    '''SELECT timestamp FROM events
                       WHERE title = "idle_end"
                       ORDER BY timestamp DESC LIMIT 1'''
                ) as cur:
                    row = await cur.fetchone()

                if row:
                    idle_end_time = datetime.fromisoformat(row["timestamp"])
                    mins_since    = (datetime.now() - idle_end_time).total_seconds() / 60

                    # Show nudge within 5 minutes of returning from idle
                    if mins_since <= 5:
                        # Get the last memory node for context
                        async with db.execute(
                            '''SELECT summary, files_touched
                               FROM memory_nodes
                               ORDER BY timestamp DESC LIMIT 1'''
                        ) as cur:
                            node_row = await cur.fetchone()

                        if node_row:
                            summary = node_row["summary"] or "your last session"
                            alerts.append(Alert(
                                alert_type = "focus_nudge",
                                title      = f"↩ Welcome back",
                                body       = f"You were: {summary}",
                                priority   = 3,  # Highest priority
                                data       = {"summary": summary}
                            ))
            except Exception:
                pass

    # Sort by priority — highest first
    alerts.sort(key=lambda a: a.priority, reverse=True)
    return alerts


async def get_context_restore(app_name: str) -> Optional[Alert]:
    """
    Called when user switches to an app they haven't used recently.
    Returns a context restore alert with what they were doing there.

    Used by bar.py which monitors window focus changes.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Find the most recent memory node that involved this app
        async with db.execute(
            '''SELECT summary, timestamp, files_touched
               FROM memory_nodes
               WHERE apps_used LIKE ?
               ORDER BY timestamp DESC LIMIT 1''',
            (f'%{app_name.lower().replace(".exe", "")}%',)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        node_time = datetime.fromisoformat(row["timestamp"])
        mins_ago  = (datetime.now() - node_time).total_seconds() / 60

        # Only restore if more than 30 minutes ago (otherwise not needed)
        if mins_ago < 30:
            return None

        # Format time ago
        if mins_ago < 120:
            time_str = f"{int(mins_ago)}m ago"
        elif mins_ago < 1440:
            time_str = f"{int(mins_ago/60)}h ago"
        else:
            time_str = f"{int(mins_ago/1440)}d ago"

        summary = row["summary"] or "previous session"
        files   = []
        try:
            files = json.loads(row["files_touched"] or "[]")
        except Exception:
            pass

        body = f"Last session ({time_str}): {summary}"
        if files:
            fname = files[0].replace("\\", "/").split("/")[-1]
            body += f" · Last file: {fname}"

        return Alert(
            alert_type = "context_restore",
            title      = f"↩ {app_name.replace('.exe','')} — {time_str}",
            body       = body,
            priority   = 3,
            data       = {"app": app_name, "summary": summary}
        )