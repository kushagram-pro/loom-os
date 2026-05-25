# surface/bar.py
#
# WHAT THIS FILE DOES:
# The Loom Bar — a slim, always-present overlay at the top of the screen.
# Built with PyQt6 for full visual control.
#
# THREE STATES:
# 1. Collapsed  → 36px tall, shows current project + focus dot
# 2. Expanded   → 220px tall, shows full digest and alerts
# 3. Alert      → bar briefly pulses accent color, shows alert title
#
# VISUAL DESIGN:
# - Dark background (#0a0a0b) matching Loom brand
# - Accent green (#c8f04a) for active indicators
# - DM Mono font throughout
# - Frameless, transparent window — no Windows title bar
# - Always on top, non-focusable (doesn't steal keyboard focus)
#
# USER PREFERENCES:
# Dismiss duration stored in SQLite alert_preferences table.
# User can right-click the bar to open settings.
#
# HOW IT TALKS TO THE REST OF LOOM:
# Runs its own asyncio loop in a background thread.
# Polls for new digest and alerts every 60 seconds.
# Listens for window focus changes to trigger context restores.


import asyncio
import sys
import os
import threading
import json
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout,
    QVBoxLayout, QPushButton, QFrame, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QTimer, QPoint, pyqtSignal, QObject,
    QPropertyAnimation, QEasingCurve, QRect
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QCursor,
    QFontDatabase, QPainter, QBrush
)

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_SURFACE   = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_SURFACE)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')
_MEMORY    = os.path.join(_LOOM_ROOT, 'memory')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)
sys.path.insert(0, _SURFACE)

from digest import generate_digest, DigestData
from alerts import (
    init_alerts_tables, get_pending_alerts,
    log_alert_shown, log_alert_engagement, Alert
)

# ── Colors ─────────────────────────────────────────────────────────────────
C_BG         = "#0a0a0b"
C_SURFACE    = "#111113"
C_BORDER     = "#222226"
C_ACCENT     = "#c8f04a"
C_TEXT       = "#edeae4"
C_TEXT_2     = "#7a7874"
C_TEXT_3     = "#3a3835"
C_ALERT      = "#f0c44a"
C_DANGER     = "#ff6b6b"

# ── Dimensions ─────────────────────────────────────────────────────────────
BAR_HEIGHT_COLLAPSED = 36
BAR_HEIGHT_EXPANDED  = 224
ANIMATION_MS         = 220


# ── Signal bridge ──────────────────────────────────────────────────────────
# Bridges between the asyncio background thread and the Qt UI thread

class SignalBridge(QObject):
    digest_ready = pyqtSignal(object)   # DigestData
    alert_ready  = pyqtSignal(object)   # list[Alert]
    update_tick  = pyqtSignal()


# ── Loom Bar window ────────────────────────────────────────────────────────

class LoomBar(QWidget):
    """
    The main Loom Bar window.
    A frameless, always-on-top overlay at the top of the screen.
    """

    def __init__(self):
        super().__init__()

        self.bridge       = SignalBridge()
        self.digest       = None
        self.alerts       = []
        self.current_alert_idx  = 0
        self.is_expanded  = False
        self.alert_log_id = None
        self.alert_shown_at = None
        self._dragging    = False
        self._drag_pos    = QPoint()

        self._setup_window()
        self._setup_ui()
        self._connect_signals()
        self._start_background_tasks()
        self._start_ui_timer()

    # ── Window setup ───────────────────────────────────────────────────────

    def _setup_window(self):
        """
        Configures the window to be frameless, always on top,
        and positioned at the top of the screen.
        """
        screen = QApplication.primaryScreen().geometry()
        self.screen_width = screen.width()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool  # doesn't appear in taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)

    # ── UI construction ────────────────────────────────────────────────────

    def _setup_ui(self):
        """Builds all UI components."""

        # ── Outer container ───────────────────────────────────────────────
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

        outer_layout = QVBoxLayout(self.container)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # ── Collapsed bar row ─────────────────────────────────────────────
        self.collapsed_row = QFrame()
        self.collapsed_row.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.collapsed_row.setStyleSheet("background: transparent; border: none;")

        collapsed_layout = QHBoxLayout(self.collapsed_row)
        collapsed_layout.setContentsMargins(16, 0, 16, 0)
        collapsed_layout.setSpacing(0)

        # Logo + pulse dot
        self.logo_label = QLabel("◉  L·O·O·M")
        self.logo_label.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            font-weight: 500;
            letter-spacing: 3px;
            padding-right: 20px;
        """)

        # Separator
        sep1 = QLabel("|")
        sep1.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        # Current context label — main content of collapsed bar
        self.context_label = QLabel("Initializing...")
        self.context_label.setStyleSheet(f"""
            color: {C_TEXT_2};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        """)
        self.context_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred
        )

        # Separator
        sep2 = QLabel("|")
        sep2.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        # Time display
        self.time_label = QLabel()
        self.time_label.setStyleSheet(f"""
            color: {C_TEXT_3};
            font-family: 'DM Mono', monospace;
            font-size: 10px;
        """)

        # Expand button
        self.expand_btn = QPushButton("▾")
        self.expand_btn.setFixedSize(28, 28)
        self.expand_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3};
                background: transparent;
                border: none;
                font-size: 14px;
                padding: 0;
            }}
            QPushButton:hover {{
                color: {C_ACCENT};
            }}
        """)
        self.expand_btn.clicked.connect(self.toggle_expand)

        collapsed_layout.addWidget(self.logo_label)
        collapsed_layout.addWidget(sep1)
        collapsed_layout.addWidget(self.context_label)
        collapsed_layout.addWidget(sep2)
        collapsed_layout.addWidget(self.time_label)
        collapsed_layout.addSpacing(12)
        collapsed_layout.addWidget(self.expand_btn)

        # ── Expanded panel ────────────────────────────────────────────────
        self.expanded_panel = QFrame()
        self.expanded_panel.setFixedHeight(BAR_HEIGHT_EXPANDED - BAR_HEIGHT_COLLAPSED)
        self.expanded_panel.setVisible(False)
        self.expanded_panel.setStyleSheet(f"""
            QFrame {{
                background-color: {C_SURFACE};
                border-bottom: 1px solid {C_BORDER};
                border-top: 1px solid {C_BORDER};
            }}
        """)

        expanded_layout = QVBoxLayout(self.expanded_panel)
        expanded_layout.setContentsMargins(20, 14, 20, 14)
        expanded_layout.setSpacing(8)

        # Digest section
        self.digest_label = QLabel("Loading your memory...")
        self.digest_label.setStyleSheet(f"""
            color: {C_TEXT};
            font-family: 'DM Mono', monospace;
            font-size: 12px;
            line-height: 1.6;
        """)
        self.digest_label.setWordWrap(True)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {C_BORDER}; background: {C_BORDER}; max-height: 1px;")

        # Alert section
        self.alert_label = QLabel("")
        self.alert_label.setStyleSheet(f"""
            color: {C_ALERT};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            line-height: 1.5;
        """)
        self.alert_label.setWordWrap(True)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3};
                background: transparent;
                border: 1px solid {C_BORDER};
                border-radius: 5px;
                padding: 5px 14px;
                font-family: 'DM Mono', monospace;
                font-size: 10px;
            }}
            QPushButton:hover {{
                color: {C_TEXT};
                border-color: {C_TEXT_3};
            }}
        """)
        self.dismiss_btn.clicked.connect(self.dismiss)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3};
                background: transparent;
                border: none;
                padding: 5px 10px;
                font-family: 'DM Mono', monospace;
                font-size: 10px;
            }}
            QPushButton:hover {{
                color: {C_TEXT_2};
            }}
        """)
        self.settings_btn.clicked.connect(self.open_settings)

        action_row.addWidget(self.dismiss_btn)
        action_row.addStretch()
        action_row.addWidget(self.settings_btn)

        expanded_layout.addWidget(self.digest_label)
        expanded_layout.addWidget(divider)
        expanded_layout.addWidget(self.alert_label)
        expanded_layout.addLayout(action_row)

        # ── Assemble ──────────────────────────────────────────────────────
        outer_layout.addWidget(self.collapsed_row)
        outer_layout.addWidget(self.expanded_panel)

    # ── Signal connections ─────────────────────────────────────────────────

    def _connect_signals(self):
        self.bridge.digest_ready.connect(self._on_digest_ready)
        self.bridge.alert_ready.connect(self._on_alerts_ready)
        self.bridge.update_tick.connect(self._update_time)

    # ── Background async tasks ─────────────────────────────────────────────

    def _start_background_tasks(self):
        """
        Starts the asyncio event loop in a background thread.
        This lets us run async database queries without blocking the UI.
        """
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_init())
            loop.run_until_complete(self._async_polling_loop())

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

    async def _async_init(self):
        """One-time async setup — init tables and load first digest."""
        await init_alerts_tables()
        await self._load_digest()
        await self._load_alerts()

    async def _async_polling_loop(self):
        """Polls for new digest and alerts every 60 seconds."""
        while True:
            await asyncio.sleep(60)
            await self._load_digest()
            await self._load_alerts()

    async def _load_digest(self):
        """Generates fresh digest and emits signal to UI thread."""
        try:
            digest = await generate_digest()
            self.bridge.digest_ready.emit(digest)
        except Exception as e:
            print(f"[Bar] Digest error: {e}")

    async def _load_alerts(self):
        """Loads pending alerts and emits signal to UI thread."""
        try:
            alerts = await get_pending_alerts()
            self.bridge.alert_ready.emit(alerts)
        except Exception as e:
            print(f"[Bar] Alerts error: {e}")

    # ── UI timer ───────────────────────────────────────────────────────────

    def _start_ui_timer(self):
        """Updates the time display every 30 seconds."""
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_time)
        self.ui_timer.start(30000)
        self._update_time()

    def _update_time(self):
        self.time_label.setText(
            datetime.now().strftime("%H:%M  %a %d %b")
        )

    # ── Signal handlers ────────────────────────────────────────────────────

    def _on_digest_ready(self, digest: DigestData):
        """Updates UI with fresh digest data."""
        self.digest = digest

        # Update collapsed bar context
        if digest.current_project:
            ctx = f"{digest.current_project}  ·  {digest.last_session_ago}"
        else:
            ctx = digest.last_worked_on[:60] if digest.last_worked_on else "Ready"
        self.context_label.setText(ctx)

        # Update expanded panel
        lines = []
        lines.append(f"{digest.greeting}")
        lines.append(f"")
        lines.append(f"Last session:  {digest.last_worked_on}")

        if digest.last_file:
            lines.append(f"Last file:     {digest.last_file}  ·  {digest.last_session_ago}")

        if digest.focus_today:
            lines.append(f"")
            lines.append(f"Today:  {digest.focus_today}")

        self.digest_label.setText("\n".join(lines))

    def _on_alerts_ready(self, alerts: list):
        """Updates alert display with pending alerts."""
        self.alerts = alerts
        self.current_alert_idx = 0

        if alerts:
            self._show_current_alert()
            # Pulse the bar to signal new alert
            self._pulse_alert()

    def _show_current_alert(self):
        """Shows the current alert in the expanded panel."""
        if not self.alerts or self.current_alert_idx >= len(self.alerts):
            self.alert_label.setText("")
            return

        alert = self.alerts[self.current_alert_idx]
        self.alert_label.setText(f"{alert.title}\n{alert.body}")
        self.alert_shown_at = datetime.now()

        # Log that this alert was shown
        def log():
            loop = asyncio.new_event_loop()
            self.alert_log_id = loop.run_until_complete(
                log_alert_shown(alert.alert_type, alert.title)
            )
        threading.Thread(target=log, daemon=True).start()

    # ── Visual effects ─────────────────────────────────────────────────────

    def _pulse_alert(self):
        """Briefly highlights the bar to signal a new alert."""
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_ACCENT};
            }}
        """)
        QTimer.singleShot(2000, self._reset_border)

    def _reset_border(self):
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

    # ── Expand / collapse ──────────────────────────────────────────────────

    def toggle_expand(self):
        """Toggles between collapsed and expanded state."""
        if self.is_expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self):
        """Expands the bar to show full digest and alerts."""
        self.is_expanded = True
        self.expanded_panel.setVisible(True)
        self.container.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.expand_btn.setText("▴")

        # Log engagement if there's an active alert
        if self.alert_log_id and self.alert_shown_at:
            visible = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    log_alert_engagement(self.alert_log_id, True, visible)
                )
            threading.Thread(target=log, daemon=True).start()

    def collapse(self):
        """Collapses back to the slim bar."""
        self.is_expanded = False
        self.expanded_panel.setVisible(False)
        self.container.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.expand_btn.setText("▾")

    def dismiss(self):
        """Dismisses the current alert and collapses."""
        if self.alert_log_id and self.alert_shown_at:
            visible = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    log_alert_engagement(self.alert_log_id, False, visible)
                )
            threading.Thread(target=log, daemon=True).start()

        self.collapse()

    def open_settings(self):
        """Opens a simple settings dialog."""
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Loom Settings")
        msg.setText(
            "Loom Bar Settings\n\n"
            "Alert preferences are learned automatically\n"
            "from your engagement patterns.\n\n"
            "Engage with alerts you find useful.\n"
            "Dismiss ones you don't — Loom will show\n"
            "them less frequently over time."
        )
        msg.setStyleSheet(f"background: {C_BG}; color: {C_TEXT};")
        msg.exec()

    # ── Mouse events — drag to reposition ─────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging  = True
            self._drag_pos  = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._dragging:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            # Constrain to top of screen
            new_pos.setY(max(0, min(new_pos.y(), 200)))
            self.move(new_pos)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def mouseDoubleClickEvent(self, event):
        self.toggle_expand()