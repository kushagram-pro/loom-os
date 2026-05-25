# screen_context.py
# Uses Windows UI Automation to read visible text context
# from whatever application is currently on screen.
# Works on ANY app — no plugin needed.
# Polls every 10 seconds — reads semantic content, not keystrokes.

import asyncio
import uiautomation as auto
from datetime import datetime

# Apps where reading UI content is particularly valuable
HIGH_VALUE_APPS = {
    'code', 'cursor', 'notepad', 'notion',
    'word', 'obsidian', 'chrome', 'edge',
    'firefox', 'slack', 'teams', 'terminal',
    'windowsterminal', 'powershell'
}


def read_screen_context() -> dict | None:
    """
    Reads the focused UI element and its surrounding context
    from whatever app is currently active.

    UI Automation gives us access to the accessibility tree —
    the same tree screen readers use. Every app exposes this.
    """
    try:
        # Get the currently focused control
        focused = auto.GetFocusedControl()
        if not focused:
            return None

        # Walk up to find the root window
        root = focused
        while root.GetParentControl():
            parent = root.GetParentControl()
            if parent.ControlType == auto.ControlType.WindowControl:
                break
            root = parent

        app_name = root.Name or ""
        focused_name = focused.Name or ""
        focused_value = ""

        # Try to read the value of the focused element
        # (text field content, document name, etc.)
        try:
            value_pattern = focused.GetValuePattern()
            if value_pattern:
                val = value_pattern.Value
                # Trim to 300 chars — enough context, not too much
                focused_value = val[:300] if val else ""
        except Exception:
            pass

        if not app_name and not focused_name:
            return None

        return {
            "app": app_name,
            "focused_element": focused_name,
            "content_preview": focused_value
        }

    except Exception:
        return None


async def watch_screen_context(queue: asyncio.Queue):
    """
    Every 10 seconds, reads what's on screen and logs
    a context snapshot. Less frequent than window watcher
    because UI Automation is more expensive to call.
    """
    print("[Screen context watcher] Starting...")

    last_context = ""

    while True:
        context = read_screen_context()

        if context:
            # Only log if something meaningful changed
            context_key = f"{context['app']}|{context['focused_element']}"

            if context_key != last_context:
                event = {
                    "timestamp": datetime.now().isoformat(),
                    "source": "screen",
                    "app": context["app"],
                    "title": context["focused_element"],
                    "detail": context["content_preview"],
                    "duration": 0
                }
                await queue.put(event)
                last_context = context_key

        await asyncio.sleep(10)