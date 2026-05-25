# main.py
# Entry point for Loom's capture layer.
# Spins up all watchers and the event processor as concurrent async tasks.
# Everything flows through a single shared asyncio.Queue.

import asyncio
import sys
import os

# Add capture/ to path so sibling imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

from database import init_db
from processor import process_events
from watchers.system_events import watch_system_events
from watchers.activity_rhythm import watch_activity_rhythm
from watchers.screen_context import watch_screen_context
from watchers.clipboard import watch_clipboard
from watchers.vscode import watch_vscode


async def main():
    print("=" * 50)
    print("  Loom — activity capture starting")
    print("=" * 50)

    # Initialise the database (creates tables if needed)
    await init_db()
    print("[DB] Initialised — loom.db ready")

    # Single shared queue — all watchers write, processor reads
    queue: asyncio.Queue = asyncio.Queue()

    # Launch every watcher + the processor concurrently
    tasks = [
        asyncio.create_task(watch_system_events(queue),   name="system"),
        asyncio.create_task(watch_activity_rhythm(queue), name="rhythm"),
        asyncio.create_task(watch_screen_context(queue),  name="screen"),
        asyncio.create_task(watch_clipboard(queue),       name="clipboard"),
        asyncio.create_task(watch_vscode(queue),          name="vscode"),
        asyncio.create_task(process_events(queue),        name="processor"),
    ]

    print(f"[Main] {len(tasks)} tasks running — press Ctrl+C to stop\n")

    try:
        # Run forever; any task raising an exception surfaces here
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n[Main] Shutting down...")
    except Exception as e:
        print(f"[Main] Fatal error: {e}")
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("[Main] All tasks stopped.")


if __name__ == "__main__":
    asyncio.run(main())
