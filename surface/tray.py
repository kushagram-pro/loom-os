# surface/tray.py
#
# WHAT THIS FILE DOES:
# Creates the Loom system tray icon.
# Right-click menu gives quick access to:
# - Show/hide the bar
# - Run a manual sync
# - View stats
# - Quit Loom
#
# The tray icon is the always-present indicator that Loom is running.
# Even if the bar is hidden, the tray icon stays.


import sys
import os
import asyncio
import threading
from datetime import datetime

from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QBrush, QPen
from PyQt6.QtCore import Qt, QSize

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_SURFACE   = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_SURFACE)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')
_MEMORY    = os.path.join(_LOOM_ROOT, 'memory')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)


def create_loom_icon(color: str = "#c8f04a") -> QIcon:
    """
    Creates the Loom tray icon programmatically.
    A simple circle with the accent color — no image file needed.
    """
    size   = 22
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Outer ring
    pen = QPen(QColor(color))
    pen.setWidth(2)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    # Inner dot
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(color)))
    center = size // 2
    painter.drawEllipse(center - 3, center - 3, 6, 6)

    painter.end()
    return QIcon(pixmap)


class LoomTray:
    """
    System tray icon for Loom.
    Provides a persistent indicator and quick-access menu.
    """

    def __init__(self, bar_window):
        self.bar    = bar_window
        self.tray   = QSystemTrayIcon()
        self.tray.setIcon(create_loom_icon("#c8f04a"))
        self.tray.setToolTip("Loom — Living Overlay Of Memory")

        self._build_menu()

        self.tray.activated.connect(self._on_tray_click)
        self.tray.show()

    def _build_menu(self):
        """Builds the right-click context menu."""
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #111113;
                color: #edeae4;
                border: 1px solid #222226;
                padding: 4px;
                font-family: 'DM Mono', monospace;
                font-size: 11px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #1e1e22;
                color: #c8f04a;
            }
            QMenu::separator {
                height: 1px;
                background: #222226;
                margin: 4px 0;
            }
        """)

        # Show/hide bar
        self.toggle_action = menu.addAction("Hide bar")
        self.toggle_action.triggered.connect(self._toggle_bar)

        menu.addSeparator()

        # Quick queries
        week_action = menu.addAction("This week's work")
        week_action.triggered.connect(self._show_week)

        stale_action = menu.addAction("Stale projects")
        stale_action.triggered.connect(self._show_stale)

        menu.addSeparator()

        # Stats
        stats_action = menu.addAction("Memory stats")
        stats_action.triggered.connect(self._show_stats)

        menu.addSeparator()

        # Quit
        quit_action = menu.addAction("Quit Loom")
        quit_action.triggered.connect(QApplication.quit)

        self.tray.setContextMenu(menu)
        self.menu = menu

    def _on_tray_click(self, reason):
        """Single click on tray icon toggles bar visibility."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_bar()

    def _toggle_bar(self):
        if self.bar.isVisible():
            self.bar.hide()
            self.toggle_action.setText("Show bar")
            self.tray.setIcon(create_loom_icon("#3a3835"))  # dim when hidden
        else:
            self.bar.show()
            self.toggle_action.setText("Hide bar")
            self.tray.setIcon(create_loom_icon("#c8f04a"))

    def _show_week(self):
        """Shows this week's work in a tray notification."""
        def run():
            loop = asyncio.new_event_loop()
            try:
                from query import query_this_week
                # Capture output and show as notification
                self.tray.showMessage(
                    "Loom — This Week",
                    "Opening memory query...",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000
                )
            except Exception as e:
                self.tray.showMessage("Loom", str(e),
                    QSystemTrayIcon.MessageIcon.Warning, 3000)

        threading.Thread(target=run, daemon=True).start()

    def _show_stale(self):
        """Shows stale project count in tray notification."""
        def run():
            loop = asyncio.new_event_loop()
            try:
                import aiosqlite
                from database import DB_PATH

                async def get_stale():
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute(
                            'SELECT COUNT(*) FROM projects WHERE is_stale = 1'
                        ) as cur:
                            return (await cur.fetchone())[0]

                count = loop.run_until_complete(get_stale())
                msg   = f"{count} stale project(s) detected" if count > 0 else "No stale projects"
                self.tray.showMessage("Loom — Stale Projects", msg,
                    QSystemTrayIcon.MessageIcon.Information, 3000)
            except Exception as e:
                self.tray.showMessage("Loom", f"Error: {e}",
                    QSystemTrayIcon.MessageIcon.Warning, 3000)

        threading.Thread(target=run, daemon=True).start()

    def _show_stats(self):
        """Shows memory graph stats in a tray notification."""
        def run():
            loop = asyncio.new_event_loop()
            try:
                import aiosqlite
                from database import DB_PATH

                async def get_stats():
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute('SELECT COUNT(*) FROM events') as cur:
                            events = (await cur.fetchone())[0]
                        async with db.execute('SELECT COUNT(*) FROM memory_nodes') as cur:
                            nodes = (await cur.fetchone())[0]
                    return events, nodes

                events, nodes = loop.run_until_complete(get_stats())
                msg = f"{events} events captured · {nodes} memory nodes"
                self.tray.showMessage("Loom — Memory Stats", msg,
                    QSystemTrayIcon.MessageIcon.Information, 4000)
            except Exception as e:
                self.tray.showMessage("Loom", f"Error: {e}",
                    QSystemTrayIcon.MessageIcon.Warning, 3000)

        threading.Thread(target=run, daemon=True).start()