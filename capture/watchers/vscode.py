# vscode.py
# Tracks VS Code activity at a deeper level than the system watcher.
# The system watcher already captures window focus; this adds:
#   - which file is being edited (parsed from window title)
#   - language/extension of the active file
#   - file-save events (via watchdog on active workspace dirs)
#
# VS Code window title format: "filename — folder [workspace] - Visual Studio Code"

import asyncio
import os
import re
import sqlite3
import win32gui
import win32process
import psutil
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

VSCODE_EXE = {"code.exe", "cursor.exe"}

# Resolve VS Code's recently opened workspaces from its state DB
VSCODE_STATE_DB = os.path.expandvars(
    r"%APPDATA%\Code\User\globalStorage\state.vscdb"
)
CURSOR_STATE_DB = os.path.expandvars(
    r"%APPDATA%\Cursor\User\globalStorage\state.vscdb"
)

# Ignore saves in these directories — they're editor internals
IGNORE_DIR_FRAGMENTS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}

# Only report saves for these extensions (source files)
SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".cpp", ".c", ".h", ".cs", ".rb", ".php", ".swift", ".kt",
    ".md", ".json", ".yaml", ".yml", ".toml", ".env", ".sql",
    ".html", ".css", ".scss", ".vue", ".svelte"
}


def _parse_active_file(title: str) -> dict | None:
    """
    Extract filename and folder from VS Code window title.
    Handles common formats:
      - "file.py — project - Visual Studio Code"
      - "● file.py — project - Visual Studio Code"  (unsaved indicator)
      - "project - Visual Studio Code"              (no file open)
    """
    # Strip the app suffix
    title = re.sub(r'\s*-\s*Visual Studio Code.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*-\s*Cursor.*$', '', title, flags=re.IGNORECASE)
    title = title.strip()

    # Split on em-dash separator
    parts = re.split(r'\s*[—–]\s*', title)

    if len(parts) >= 2:
        raw_file = parts[0].strip().lstrip('●').strip()
        folder = parts[1].strip()
        _, ext = os.path.splitext(raw_file)
        return {
            "file": raw_file,
            "folder": folder,
            "extension": ext.lower()
        }
    elif len(parts) == 1 and parts[0]:
        return {"file": "", "folder": parts[0], "extension": ""}

    return None


def _get_vscode_workspaces() -> list[str]:
    """
    Read recently opened workspace folders from VS Code's SQLite state DB.
    Returns a list of local folder paths.
    """
    paths = []

    for db_path in (VSCODE_STATE_DB, CURSOR_STATE_DB):
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'history.recentlyOpenedPathsList'"
            )
            row = cursor.fetchone()
            conn.close()

            if not row:
                continue

            import json
            data = json.loads(row[0])
            entries = data.get("entries", [])

            for entry in entries:
                folder_uri = entry.get("folderUri", "")
                if folder_uri.startswith("file:///"):
                    # Convert URI to Windows path
                    local = folder_uri[8:].replace("/", os.sep)
                    if os.path.isdir(local):
                        paths.append(local)

        except Exception:
            pass

    return paths


class _SaveHandler(FileSystemEventHandler):
    """Watchdog handler: queues an event when a source file is saved."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._queue = queue
        self._loop = loop

    def on_modified(self, event):
        if event.is_directory:
            return

        path = event.src_path
        _, ext = os.path.splitext(path)

        if ext.lower() not in SOURCE_EXTENSIONS:
            return

        # Skip editor-internal directories
        parts = path.replace("\\", "/").split("/")
        if any(frag in parts for frag in IGNORE_DIR_FRAGMENTS):
            return

        ev = {
            "timestamp": datetime.now().isoformat(),
            "source": "vscode",
            "app": "code",
            "title": "file_save",
            "detail": path,
            "duration": 0,
            "file": os.path.basename(path),
            "extension": ext.lower(),
        }
        asyncio.run_coroutine_threadsafe(self._queue.put(ev), self._loop)


def _get_foreground_exe() -> str:
    """Return the exe name of the foreground window, lower-cased."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


async def watch_vscode(queue: asyncio.Queue):
    """
    Two parallel jobs:
      1. Poll the VS Code foreground window title every 5 s to track the active file.
      2. Use watchdog to emit file-save events from open workspace directories.
    """
    print("[VS Code watcher] Starting — tracking active file and saves...")

    loop = asyncio.get_event_loop()
    last_file_key = ""

    # --- Watchdog setup ---
    observer = Observer()
    handler = _SaveHandler(queue, loop)
    watched_dirs: set[str] = set()

    def refresh_watches():
        workspaces = _get_vscode_workspaces()
        for folder in workspaces:
            if folder not in watched_dirs:
                try:
                    observer.schedule(handler, folder, recursive=True)
                    watched_dirs.add(folder)
                    print(f"[VS Code watcher] Watching: {folder}")
                except Exception:
                    pass

    refresh_watches()
    observer.start()

    # --- Polling loop ---
    tick = 0
    while True:
        await asyncio.sleep(5)
        tick += 1

        # Refresh workspace watches every 5 minutes
        if tick % 60 == 0:
            refresh_watches()

        # Only parse window title when VS Code is in front
        exe = _get_foreground_exe()
        if exe not in VSCODE_EXE:
            continue

        try:
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            continue

        info = _parse_active_file(title)
        if not info or not info.get("file"):
            continue

        file_key = f"{info['folder']}|{info['file']}"
        if file_key == last_file_key:
            continue

        last_file_key = file_key

        event = {
            "timestamp": datetime.now().isoformat(),
            "source": "vscode",
            "app": exe.replace(".exe", ""),
            "title": info["file"],
            "detail": info["folder"],
            "duration": 0,
            "extension": info["extension"],
        }
        await queue.put(event)
