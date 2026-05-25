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


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║       L · O · O · M                         ║")
    print("  ║       Surface Layer                          ║")
    print("  ║       Living Overlay Of Memory               ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()

    # Register auto-start (only runs once, checks if already registered)
    install_autostart()

    # Create Qt application
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running even if bar is hidden

    # Check system tray is available
    if not hasattr(app, 'screenAt'):
        print("[Surface] Warning: system tray may not be available")

    # Create the Loom Bar
    bar = LoomBar()
    bar.show()

    # Create the system tray icon
    tray = LoomTray(bar)

    print("[Surface] Loom Bar is running")
    print("[Surface] Look for the bar at the top of your screen")
    print("[Surface] Right-click the tray icon for options")
    print("[Surface] Press Ctrl+C here or use tray menu to quit")
    print()

    # Run the Qt event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()