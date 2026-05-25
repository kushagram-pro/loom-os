# system_events.py
# Uses Windows SetWinEventHook to listen to focus changes
# across EVERY application on the system — no app list needed.
# This is how Windows accessibility tools work internally.

import asyncio
import win32con
import win32gui
import win32process
import psutil
import ctypes
import ctypes.wintypes
from datetime import datetime

# Windows event constant — fires when foreground window changes
EVENT_SYSTEM_FOREGROUND = 0x0003

# System processes to ignore — these are Windows internals
SYSTEM_PROCESSES = {
    'searchhost.exe', 'shellexperiencehost.exe',
    'startmenuexperiencehost.exe', 'textinputhost.exe',
    'applicationframehost.exe', 'systemsettings.exe',
    'explorer.exe', 'taskmgr.exe', 'lockapp.exe'
}


def get_window_info(hwnd) -> dict | None:
    """
    Given a window handle, returns structured info about it.
    Works for ANY application — not just ones we hardcoded.
    """
    try:
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return None

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        proc_name = process.name().lower()

        # Skip system processes
        if proc_name in SYSTEM_PROCESSES:
            return None

        return {
            "app": process.name(),
            "app_clean": proc_name.replace('.exe', ''),
            "title": title,
            "pid": pid
        }

    except Exception:
        return None


async def watch_system_events(queue: asyncio.Queue):
    """
    Sets up a Windows hook that fires every time the user
    switches focus to any window on the entire system.

    Uses a Windows message loop — required for hooks to work.
    Runs in an executor thread so it doesn't block asyncio.
    """
    print("[System watcher] Starting — watching all applications...")

    loop = asyncio.get_event_loop()

    last_info = None
    session_start = datetime.now()

    # This is the callback Windows calls on every focus change
    def win_event_callback(hWinEventHook, event, hwnd,
                           idObject, idChild, dwEventThread, dwmsEventTime):
        nonlocal last_info, session_start

        info = get_window_info(hwnd)
        if not info:
            return

        now = datetime.now()

        # Log the previous window's session duration
        if last_info:
            duration = (now - session_start).total_seconds()

            # Only log sessions longer than 4 seconds — filters flickers
            if duration >= 4:
                event_data = {
                    "timestamp": now.isoformat(),
                    "source": "system",
                    "app": last_info["app"],
                    "title": last_info["title"],
                    "detail": last_info["app_clean"],
                    "duration": round(duration, 1)
                }
                # Bridge from sync callback to async queue
                asyncio.run_coroutine_threadsafe(
                    queue.put(event_data), loop
                )

        last_info = info
        session_start = now

    # Create the Windows hook
    # WinEventProc is the function signature Windows expects
    WinEventProc = ctypes.WINFUNCTYPE(
        None,
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LONG,
        ctypes.wintypes.LONG,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD
    )

    proc = WinEventProc(win_event_callback)

    # Register the hook with Windows
    hook = ctypes.windll.user32.SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND,  # event type to watch
        EVENT_SYSTEM_FOREGROUND,
        0,                        # all processes
        proc,
        0,                        # all processes
        0,                        # all threads
        win32con.WINEVENT_OUTOFCONTEXT
    )

    # Windows message pump — required to receive hook callbacks
    # Runs in a thread via run_in_executor
    def message_loop():
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(
            ctypes.byref(msg), 0, 0, 0
        ) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    await loop.run_in_executor(None, message_loop)