# activity_rhythm.py
# Tracks WHEN you're active, not WHAT you're doing.
# Uses pynput to detect keyboard and mouse activity globally.
# Builds a picture of your focus rhythm — deep work vs idle.
# IMPORTANT: This never logs what keys you pressed.
# It only logs activity/idle transitions.

import asyncio
from pynput import keyboard, mouse
from datetime import datetime

# How many seconds of no input = considered idle
IDLE_THRESHOLD_SECONDS = 60

# Minimum activity burst to log (seconds of continuous activity)
MIN_BURST_SECONDS = 30


async def watch_activity_rhythm(queue: asyncio.Queue):
    """
    Detects typing and mouse activity patterns.
    Logs two types of events:
    - focus_burst: period of sustained activity (deep work signal)
    - idle_start: user went idle (context switch signal)

    Uses pynput which works across ALL applications globally.
    """
    print("[Activity watcher] Starting — tracking focus rhythm...")

    loop = asyncio.get_event_loop()

    last_activity = datetime.now()
    burst_start = datetime.now()
    is_idle = False

    def on_activity(*args):
        """Called on any keyboard or mouse event."""
        nonlocal last_activity, burst_start, is_idle

        now = datetime.now()
        idle_duration = (now - last_activity).total_seconds()

        # If returning from idle — log idle end, start new burst
        if idle_duration > IDLE_THRESHOLD_SECONDS:
            if not is_idle:
                idle_event = {
                    "timestamp": now.isoformat(),
                    "source": "rhythm",
                    "app": "",
                    "title": "idle_end",
                    "detail": f"idle for {round(idle_duration)}s",
                    "duration": round(idle_duration, 1)
                }
                asyncio.run_coroutine_threadsafe(
                    queue.put(idle_event), loop
                )
            burst_start = now
            is_idle = False

        last_activity = now

    async def check_idle():
        """
        Periodically checks if user has gone idle.
        Runs as a separate async task alongside the listeners.
        """
        nonlocal is_idle, burst_start

        while True:
            await asyncio.sleep(15)
            now = datetime.now()
            idle_duration = (now - last_activity).total_seconds()

            # User just went idle
            if idle_duration > IDLE_THRESHOLD_SECONDS and not is_idle:
                burst_duration = (last_activity - burst_start).total_seconds()

                # Only log if the burst was meaningful
                if burst_duration >= MIN_BURST_SECONDS:
                    burst_event = {
                        "timestamp": now.isoformat(),
                        "source": "rhythm",
                        "app": "",
                        "title": "focus_burst",
                        "detail": f"active for {round(burst_duration)}s",
                        "duration": round(burst_duration, 1)
                    }
                    await queue.put(burst_event)

                idle_event = {
                    "timestamp": now.isoformat(),
                    "source": "rhythm",
                    "app": "",
                    "title": "idle_start",
                    "detail": "",
                    "duration": 0
                }
                await queue.put(idle_event)
                is_idle = True

    # Start the idle checker as a background task
    asyncio.create_task(check_idle())

    # Start global listeners — work across all apps
    kb_listener = keyboard.Listener(on_press=on_activity)
    mouse_listener = mouse.Listener(
        on_move=on_activity,
        on_click=on_activity,
        on_scroll=on_activity
    )

    kb_listener.start()
    mouse_listener.start()

    # Keep alive
    while True:
        await asyncio.sleep(1)