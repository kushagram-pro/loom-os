# compression/scheduler.py
#
# WHAT THIS FILE DOES:
# Runs the compression engine every 30 minutes. Forever.
# This is the only file you need to start and leave running.
#
# HOW TO RUN:
#   Open a NEW terminal (capture service stays in its own terminal)
#   cd loom/compression
#   python scheduler.py
#
# YOU WILL NOW HAVE TWO TERMINALS RUNNING:
#   Terminal 1: python capture/main.py      ← capturing events
#   Terminal 2: python compression/scheduler.py ← compressing every 30 min
#
# WHY TWO SEPARATE PROCESSES:
# Compression is CPU-heavy — it runs a local LLM.
# Capture must be lightweight and always-on.
# Running them separately means a slow compression run
# never causes the capture service to miss events.
#
# WHAT YOU'LL SEE:
# Every 30 minutes the scheduler fires a compression run.
# You'll see the summary, intent, and keywords printed to terminal.
# If no meaningful activity happened — it skips gracefully.
#
# STOPPING:
# Ctrl+C in this terminal. The capture service keeps running.


import asyncio
import sys
import os
from datetime import datetime, timedelta

# ── Fix Windows path resolution ───────────────────────────────────────────
_THIS_FILE   = os.path.abspath(__file__)
_COMPRESSION = os.path.dirname(_THIS_FILE)
_LOOM_ROOT   = os.path.dirname(_COMPRESSION)
_CAPTURE     = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _COMPRESSION)

from engine import run_compression, init_memory_table, get_recent_memory_nodes

# ── Configuration ──────────────────────────────────────────────────────────
COMPRESSION_INTERVAL_MINUTES = 30
COMPRESSION_INTERVAL_SECONDS = COMPRESSION_INTERVAL_MINUTES * 60


# ── Banner ─────────────────────────────────────────────────────────────────

def print_banner():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║       L · O · O · M                         ║")
    print("  ║       Compression Engine                     ║")
    print(f"  ║       Started: {now}          ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    print(f"  Model:     phi4-mini via Ollama")
    print(f"  Interval:  every {COMPRESSION_INTERVAL_MINUTES} minutes")
    print(f"  Database:  loom_events.db → memory_nodes table")
    print(f"  Stop:      Ctrl+C")
    print()


# ── Startup summary ────────────────────────────────────────────────────────

async def print_existing_nodes():
    """
    On startup, shows the most recent memory nodes already in the database.
    Gives you an immediate sense of what Loom already knows.
    """
    try:
        nodes = await get_recent_memory_nodes(limit=3)
        if not nodes:
            print("[Scheduler] No memory nodes yet — first run will create them.")
            return

        print(f"[Scheduler] Existing memory nodes ({len(nodes)} most recent):\n")
        for node in nodes:
            ts      = node.get("timestamp", "")[:16].replace("T", " ")
            summary = node.get("summary") or "—"
            focus   = node.get("focus_quality", "—")
            print(f"  {ts}  [{focus}]  {summary}")
        print()

    except Exception:
        # Table might not exist yet on very first run
        pass


# ── Countdown display ──────────────────────────────────────────────────────

async def show_next_run_countdown(seconds_until: int):
    """
    Shows a simple "next run in X minutes" message.
    Updates every 5 minutes so you can see the scheduler is alive.
    """
    check_interval = 5 * 60  # update every 5 minutes
    elapsed = 0

    while elapsed < seconds_until:
        remaining = seconds_until - elapsed
        mins = remaining // 60
        secs = remaining % 60

        if mins > 0:
            print(
                f"[Scheduler] Next compression in {mins}m {secs:02d}s  "
                f"— {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            print(
                f"[Scheduler] Next compression in {secs}s  "
                f"— {datetime.now().strftime('%H:%M:%S')}"
            )

        sleep_time = min(check_interval, seconds_until - elapsed)
        await asyncio.sleep(sleep_time)
        elapsed += sleep_time


# ── Main loop ──────────────────────────────────────────────────────────────

async def main():
    print_banner()

    # Create memory_nodes table if it doesn't exist
    await init_memory_table()

    # Show what Loom already knows
    await print_existing_nodes()

    run_number = 0

    print("[Scheduler] Starting compression loop...")
    print(f"[Scheduler] First run in 2 minutes (startup delay)\n")

    # Small startup delay — give capture service time to collect
    # some events before the first compression attempt
    await asyncio.sleep(120)

    while True:
        run_number += 1
        print(f"\n[Scheduler] ── Run #{run_number} ─────────────────────────")

        try:
            await run_compression()

        except KeyboardInterrupt:
            raise  # Let outer handler catch this

        except Exception as e:
            # Never let a single failed run kill the scheduler
            print(f"[Scheduler] ✗ Run #{run_number} failed: {e}")
            print(f"[Scheduler]   Will retry next cycle.")

        # Schedule next run
        next_run_time = (
            datetime.now() + timedelta(seconds=COMPRESSION_INTERVAL_SECONDS)
        ).strftime("%H:%M:%S")

        print(f"\n[Scheduler] Next run at {next_run_time}")

        # Wait until next compression window
        # Shows countdown so you know the scheduler is alive
        await show_next_run_countdown(COMPRESSION_INTERVAL_SECONDS)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[Scheduler] Compression engine stopped.")
        print("[Scheduler] All memory nodes safely stored.")
        print()