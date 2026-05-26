# surface/surface.py
#
# WHAT THIS FILE DOES:
# Main entry point for the Loom surface layer.
# Starts the PyQt6 application, creates the bar and tray,
# and runs the Qt event loop.
#
# HOW TO RUN:
#   cd C:\Users\kushagra\Desktop\loom
#   python surface/surface.py
#
# FOUR TERMINALS WHEN FULLY RUNNING:
#   Terminal 1: python capture/main.py
#   Terminal 2: python compression/scheduler.py
#   Terminal 3: python memory/query.py
#   Terminal 4: python surface/surface.py   ← this file
#
# AUTO-START:
# The install_autostart() function registers Loom to start
# automatically with Windows via the registry.
# Called once when surface.py runs for the first time.


import sys
import os

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_SURFACE   = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_SURFACE)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')
_MEMORY    = os.path.join(_LOOM_ROOT, 'memory')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)
sys.path.insert(0, _SURFACE)
sys.path.insert(0, r'C:\lp')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from bar  import LoomBar
from tray import LoomTray


# ── Auto-start setup ───────────────────────────────────────────────────────

def install_autostart():
    """
    Registers Loom to start automatically with Windows.
    Uses the Windows registry Run key — the standard way
    applications auto-start on Windows.

    Creates a batch file that starts all four Loom services
    in minimized windows, then registers it to run at login.

    Only runs once — checks if already registered first.
    """
    try:
        import winreg

        key_path  = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name  = "LoomAI"

        # Check if already registered
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                  winreg.KEY_READ)
            winreg.QueryValueEx(key, app_name)
            winreg.CloseKey(key)
            print("[Autostart] Already registered — skipping")
            return
        except FileNotFoundError:
            pass  # Not registered yet — continue

        # Create the startup batch file
        batch_path = os.path.join(_LOOM_ROOT, 'start_loom.bat')
        python_exe = sys.executable

        batch_content = f"""@echo off
REM Loom — Living Overlay Of Memory
REM Auto-generated startup script

REM Start capture service
start /min "" "{python_exe}" "{os.path.join(_LOOM_ROOT, 'capture', 'main.py')}"

REM Wait 3 seconds for capture to initialize
timeout /t 3 /nobreak > nul

REM Start compression scheduler
start /min "" "{python_exe}" "{os.path.join(_LOOM_ROOT, 'compression', 'scheduler.py')}"

REM Start memory graph
start /min "" "{python_exe}" "{os.path.join(_LOOM_ROOT, 'memory', 'query.py')}"

REM Wait 5 seconds then start surface
timeout /t 5 /nobreak > nul
start "" "{python_exe}" "{os.path.join(_LOOM_ROOT, 'surface', 'surface.py')}"
"""
        with open(batch_path, 'w') as f:
            f.write(batch_content)

        print(f"[Autostart] Batch file created: {batch_path}")

        # Register in Windows registry
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                              winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{batch_path}"')
        winreg.CloseKey(key)

        print(f"[Autostart] Registered '{app_name}' in Windows startup")
        print(f"[Autostart] Loom will start automatically on next login")

    except ImportError:
        print("[Autostart] winreg not available — skipping (not on Windows?)")
    except Exception as e:
        print(f"[Autostart] Failed to register: {e}")
        print(f"[Autostart] You can manually add start_loom.bat to Windows startup")


def remove_autostart():
    """
    Removes Loom from Windows auto-start.
    Call this if you want to disable auto-start.
    """
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                              winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, "LoomAI")
        winreg.CloseKey(key)
        print("[Autostart] Removed from Windows startup")
    except Exception as e:
        print(f"[Autostart] Could not remove: {e}")


# ── Service launcher ───────────────────────────────────────────────────────

def start_capture_service():
    """
    Launches capture/main.py as a background subprocess.
    Output is shown in the same terminal (no separate window).
    Returns the Popen handle so we can terminate it on exit.
    """
    import subprocess
    capture_script = os.path.join(_LOOM_ROOT, 'capture', 'main.py')

    if not os.path.exists(capture_script):
        print(f"[Surface] Capture script not found: {capture_script}")
        return None

    proc = subprocess.Popen(
        [sys.executable, capture_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    print(f"[Capture] Started (PID {proc.pid})")
    return proc


def stream_capture_output(proc):
    """
    Forwards capture service stdout to this terminal in a daemon thread.
    Stops cleanly when the process ends.
    """
    import threading

    def _forward():
        try:
            for line in proc.stdout:
                print(line, end='', flush=True)
        except Exception:
            pass

    t = threading.Thread(target=_forward, daemon=True, name="capture-log")
    t.start()


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║       L · O · O · M                         ║")
    print("  ║       Living Overlay Of Memory               ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    print("  Services started by this process:")
    print("    capture/main.py       — event capture (subprocess)")
    print("    surface/bar.py        — compression + memory sync (thread)")
    print()

    # ── Start capture service ───────────────────────────────────────────────
    capture_proc = start_capture_service()
    if capture_proc:
        stream_capture_output(capture_proc)

    # ── Register Windows auto-start (once only) ─────────────────────────────
    install_autostart()

    # ── Qt application ──────────────────────────────────────────────────────
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    bar  = LoomBar()
    bar.show()
    tray = LoomTray(bar)

    print("[Surface] Loom Bar is running — look for the bar at the top of your screen")
    print("[Surface] Right-click the tray icon to quit")
    print()

    try:
        sys.exit(app.exec())
    finally:
        # Clean up capture service when bar exits
        if capture_proc and capture_proc.poll() is None:
            print("[Surface] Stopping capture service…")
            capture_proc.terminate()
            try:
                capture_proc.wait(timeout=5)
            except Exception:
                capture_proc.kill()


if __name__ == "__main__":
    main()