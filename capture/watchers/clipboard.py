# clipboard.py
# Monitors the system clipboard for changes.
# Every time the user copies something, emit an event.
# Uses pyperclip — works across all apps on Windows.
# IMPORTANT: We log that a copy happened and a preview,
# not the full content (truncated to 200 chars).

import asyncio
import pyperclip
from datetime import datetime

POLL_INTERVAL = 1.0       # seconds between clipboard checks
CONTENT_PREVIEW_LEN = 200 # chars to keep from clipboard content


async def watch_clipboard(queue: asyncio.Queue):
    """
    Polls the clipboard every second.
    Emits an event when the content changes.
    """
    print("[Clipboard watcher] Starting — monitoring clipboard...")

    last_content = ""

    # Seed with current clipboard so we don't fire on startup
    try:
        last_content = pyperclip.paste() or ""
    except Exception:
        pass

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        try:
            current = pyperclip.paste() or ""
        except Exception:
            continue

        if current == last_content:
            continue

        last_content = current

        # Truncate to keep storage light
        preview = current[:CONTENT_PREVIEW_LEN]
        is_truncated = len(current) > CONTENT_PREVIEW_LEN

        event = {
            "timestamp": datetime.now().isoformat(),
            "source": "clipboard",
            "app": "",
            "title": "clipboard_copy",
            "detail": preview + ("…" if is_truncated else ""),
            "duration": 0,
            "char_count": len(current),
        }
        await queue.put(event)
