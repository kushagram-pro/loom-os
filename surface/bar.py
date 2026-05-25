# surface/bar.py
#
# Updated Loom Bar with richer expanded panel:
# - Focus quality progress bar
# - Deep work time today
# - Live memory stats
# - Next compression countdown
# - Work streak
# - Active project with session count


import asyncio
import sys
import os
import threading
import json
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout,
    QVBoxLayout, QPushButton, QFrame, QSizePolicy,
    QProgressBar
)
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPen, QPixmap

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
from alerts import init_alerts_tables, get_pending_alerts, log_alert_shown, log_alert_engagement

# ── Colors ─────────────────────────────────────────────────────────────────
C_BG      = "#0a0a0b"
C_SURFACE = "#111113"
C_SURFACE2= "#18181b"
C_BORDER  = "#222226"
C_BORDER2 = "#2e2e34"
C_ACCENT  = "#c8f04a"
C_TEXT    = "#edeae4"
C_TEXT_2  = "#7a7874"
C_TEXT_3  = "#3a3835"
C_ALERT   = "#f0c44a"
C_SUCCESS = "#4af0a0"
C_DANGER  = "#ff6b6b"

# ── Dimensions ─────────────────────────────────────────────────────────────
BAR_HEIGHT_COLLAPSED = 36
BAR_HEIGHT_EXPANDED  = 320


# ── Signal bridge ──────────────────────────────────────────────────────────
class SignalBridge(QObject):
    digest_ready = pyqtSignal(object)
    alert_ready  = pyqtSignal(object)


# ── Helper: styled label ───────────────────────────────────────────────────
def make_label(text, color=None, size=11, mono=True, bold=False):
    lbl = QLabel(text)
    font_family = "'DM Mono', 'Consolas', monospace" if mono else "'DM Sans', sans-serif"
    weight = "600" if bold else "400"
    lbl.setStyleSheet(f"""
        color: {color or C_TEXT_2};
        font-family: {font_family};
        font-size: {size}px;
        font-weight: {weight};
        background: transparent;
        border: none;
    """)
    return lbl


def make_row(left_text, right_text,
             left_color=None, right_color=None,
             left_size=10, right_size=11):
    """Creates a two-column label row."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)

    left  = make_label(left_text,  left_color  or C_TEXT_3, left_size)
    left.setFixedWidth(120)
    right = make_label(right_text, right_color or C_TEXT,   right_size)
    right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    row.addWidget(left)
    row.addWidget(right)
    return row, left, right


def make_divider():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {C_BORDER}; border: none;")
    return line


# ── Loom Bar ───────────────────────────────────────────────────────────────
class LoomBar(QWidget):

    def __init__(self):
        super().__init__()
        self.bridge           = SignalBridge()
        self.digest           = None
        self.alerts           = []
        self.is_expanded      = False
        self.alert_log_id     = None
        self.alert_shown_at   = None
        self._dragging        = False
        self._drag_pos        = QPoint()

        self._setup_window()
        self._setup_ui()
        self._connect_signals()
        self._start_background_tasks()
        self._start_ui_timer()

    # ── Window ─────────────────────────────────────────────────────────────
    def _setup_window(self):
        screen = QApplication.primaryScreen().geometry()
        self.screen_width = screen.width()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)

    # ── UI ─────────────────────────────────────────────────────────────────
    def _setup_ui(self):
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Collapsed row ─────────────────────────────────────────────────
        collapsed = QFrame()
        collapsed.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        collapsed.setStyleSheet("background: transparent; border: none;")

        clo = QHBoxLayout(collapsed)
        clo.setContentsMargins(16, 0, 16, 0)
        clo.setSpacing(0)

        self.logo_lbl = QLabel("◉  L·O·O·M")
        self.logo_lbl.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            font-weight: 500;
            letter-spacing: 3px;
            padding-right: 20px;
        """)

        sep1 = QLabel("|")
        sep1.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.context_lbl = QLabel("Initializing Loom...")
        self.context_lbl.setStyleSheet(f"""
            color: {C_TEXT_2};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        """)
        self.context_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        sep2 = QLabel("|")
        sep2.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.time_lbl = QLabel()
        self.time_lbl.setStyleSheet(f"""
            color: {C_TEXT_3};
            font-family: 'DM Mono', monospace;
            font-size: 10px;
        """)

        self.expand_btn = QPushButton("▾")
        self.expand_btn.setFixedSize(28, 28)
        self.expand_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent;
                border: none; font-size: 14px; padding: 0;
            }}
            QPushButton:hover {{ color: {C_ACCENT}; }}
        """)
        self.expand_btn.clicked.connect(self.toggle_expand)

        clo.addWidget(self.logo_lbl)
        clo.addWidget(sep1)
        clo.addWidget(self.context_lbl)
        clo.addWidget(sep2)
        clo.addWidget(self.time_lbl)
        clo.addSpacing(12)
        clo.addWidget(self.expand_btn)

        # ── Expanded panel ────────────────────────────────────────────────
        self.panel = QFrame()
        self.panel.setVisible(False)
        self.panel.setStyleSheet(f"""
            QFrame {{
                background-color: {C_SURFACE};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(20, 14, 20, 14)
        pl.setSpacing(6)

        # Section: Last session
        sec1 = make_label("LAST SESSION", C_TEXT_3, 9)
        pl.addWidget(sec1)

        r1, _, self.last_session_val = make_row("Last session", "—")
        pl.addLayout(r1)

        r2, _, self.last_file_val = make_row("Last file", "—")
        pl.addLayout(r2)

        pl.addWidget(make_divider())

        # Section: Focus
        sec2 = make_label("TODAY'S FOCUS", C_TEXT_3, 9)
        pl.addWidget(sec2)

        # Focus bar row — custom layout
        focus_row = QHBoxLayout()
        focus_row.setContentsMargins(0, 0, 0, 0)
        focus_row.setSpacing(0)

        focus_key = make_label("Deep work", C_TEXT_3, 10)
        focus_key.setFixedWidth(120)

        self.focus_bar_lbl.setStyleSheet(f"""
        color: {C_ACCENT};
        font-family: 'DM Mono', 'Consolas', monospace;
        font-size: 12px;
        letter-spacing: 2px;
    """)

        self.focus_time_lbl = make_label("  0m today", C_TEXT_3, 10)

        focus_row.addWidget(focus_key)
        focus_row.addWidget(self.focus_bar_lbl)
        focus_row.addWidget(self.focus_time_lbl)
        focus_row.addStretch()
        pl.addLayout(focus_row)

        r3, _, self.focus_quality_val = make_row("Quality", "—")
        pl.addLayout(r3)

        pl.addWidget(make_divider())

        # Section: Project + Memory
        sec3 = make_label("MEMORY", C_TEXT_3, 9)
        pl.addWidget(sec3)

        r4, _, self.project_val = make_row("Active project", "—")
        pl.addLayout(r4)

        r5, _, self.streak_val = make_row("Streak", "—")
        pl.addLayout(r5)

        r6, _, self.events_val = make_row("Events captured", "—")
        pl.addLayout(r6)

        r7, _, self.nodes_val = make_row("Memory nodes", "—")
        pl.addLayout(r7)

        r8, _, self.sync_val = make_row("Next compression", "—")
        pl.addLayout(r8)

        pl.addWidget(make_divider())

        # Section: Today
        sec4 = make_label("TODAY", C_TEXT_3, 9)
        pl.addWidget(sec4)

        self.today_lbl = make_label("Keep building", C_ACCENT, 11)
        pl.addWidget(self.today_lbl)

        # Alert row
        self.alert_lbl = QLabel("")
        self.alert_lbl.setStyleSheet(f"""
            color: {C_ALERT};
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            padding-top: 4px;
        """)
        self.alert_lbl.setWordWrap(True)
        pl.addWidget(self.alert_lbl)

        # Action row
        action = QHBoxLayout()
        action.setSpacing(10)
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent;
                border: 1px solid {C_BORDER}; border-radius: 5px;
                padding: 5px 14px;
                font-family: 'DM Mono', monospace; font-size: 10px;
            }}
            QPushButton:hover {{ color: {C_TEXT}; border-color: {C_TEXT_3}; }}
        """)
        self.dismiss_btn.clicked.connect(self.dismiss)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent; border: none;
                padding: 5px 10px;
                font-family: 'DM Mono', monospace; font-size: 10px;
            }}
            QPushButton:hover {{ color: {C_TEXT_2}; }}
        """)
        self.settings_btn.clicked.connect(self.open_settings)

        action.addWidget(self.dismiss_btn)
        action.addStretch()
        action.addWidget(self.settings_btn)
        pl.addLayout(action)

        outer.addWidget(collapsed)
        outer.addWidget(self.panel)

    # ── Signals ────────────────────────────────────────────────────────────
    def _connect_signals(self):
        self.bridge.digest_ready.connect(self._on_digest)
        self.bridge.alert_ready.connect(self._on_alerts)

    # ── Background ─────────────────────────────────────────────────────────
    def _start_background_tasks(self):
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_init())
            loop.run_until_complete(self._poll_loop())
        threading.Thread(target=run, daemon=True).start()

    async def _async_init(self):
        await init_alerts_tables()
        await self._load_digest()
        await self._load_alerts()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(60)
            await self._load_digest()
            await self._load_alerts()

    async def _load_digest(self):
        try:
            self.bridge.digest_ready.emit(await generate_digest())
        except Exception as e:
            print(f"[Bar] Digest error: {e}")

    async def _load_alerts(self):
        try:
            self.bridge.alert_ready.emit(await get_pending_alerts())
        except Exception as e:
            print(f"[Bar] Alert error: {e}")

    # ── Timer ──────────────────────────────────────────────────────────────
    def _start_ui_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30000)
        self._tick()

    def _tick(self):
        self.time_lbl.setText(datetime.now().strftime("%H:%M  %a %d %b"))

        # Update compression countdown every 30s
        if self.digest:
            elapsed = (datetime.now() - datetime.fromisoformat(
                self.digest.generated_at
            )).total_seconds() / 60
            remaining = max(0, self.digest.mins_to_next_sync - int(elapsed))
            if remaining == 0:
                self.sync_val.setText("running soon...")
            else:
                self.sync_val.setText(f"in {remaining} min")

    # ── Digest handler ─────────────────────────────────────────────────────
    def _on_digest(self, d: DigestData):
        self.digest = d

        # Collapsed bar context
        if d.current_project:
            ctx = f"{d.current_project}  ·  {d.last_session_ago}"
        elif d.last_worked_on != "Nothing recorded yet":
            ctx = f"{d.last_worked_on[:55]}  ·  {d.last_session_ago}"
        else:
            ctx = "Ready — no sessions yet"
        self.context_lbl.setText(ctx)

        # Last session
        self.last_session_val.setText(d.last_worked_on[:70] if d.last_worked_on else "—")
        self.last_file_val.setText(
            f"{d.last_file}  ·  {d.last_session_ago}" if d.last_file else d.last_session_ago or "—"
        )

        # Focus bar
        self.focus_bar_lbl.setText(d.focus_bar or "░░░░░░░░░░")
        hrs  = d.focus_minutes_today // 60
        mins = d.focus_minutes_today % 60
        if hrs > 0:
            time_str = f"  {hrs}h {mins}m today"
        else:
            time_str = f"  {mins}m today"
        self.focus_time_lbl.setText(time_str)

        # Focus quality with color
        quality = d.focus_quality or "medium"
        q_color = {
            "high":   C_SUCCESS,
            "medium": C_ACCENT,
            "low":    C_TEXT_3
        }.get(quality, C_TEXT_2)
        self.focus_quality_val.setText(quality.title())
        self.focus_quality_val.setStyleSheet(f"""
            color: {q_color};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        """)

        # Project
        if d.current_project:
            self.project_val.setText(
                f"{d.current_project}  ·  {d.project_sessions} session(s) this week"
            )
        else:
            self.project_val.setText("No projects detected yet")

        # Streak
        if d.streak_days >= 2:
            self.streak_val.setText(f"{d.streak_days} days  🔥")
            self.streak_val.setStyleSheet(f"""
                color: {C_ACCENT};
                font-family: 'DM Mono', monospace;
                font-size: 11px;
            """)
        elif d.streak_days == 1:
            self.streak_val.setText("1 day — keep going")
        else:
            self.streak_val.setText("—")

        # Memory stats
        self.events_val.setText(f"{d.events_captured:,}")
        self.nodes_val.setText(f"{d.memory_nodes}  ·  {d.projects_detected} project(s)")

        # Compression countdown
        if d.mins_to_next_sync == 0:
            self.sync_val.setText("running soon...")
        else:
            self.sync_val.setText(f"in {d.mins_to_next_sync} min")

        # Today suggestion
        self.today_lbl.setText(d.focus_today)

    # ── Alert handler ──────────────────────────────────────────────────────
    def _on_alerts(self, alerts):
        self.alerts = alerts
        if alerts:
            a = alerts[0]
            self.alert_lbl.setText(f"{a.title}  ·  {a.body[:80]}")
            self._pulse()

            def log():
                loop = asyncio.new_event_loop()
                self.alert_log_id = loop.run_until_complete(
                    log_alert_shown(a.alert_type, a.title)
                )
                self.alert_shown_at = datetime.now()
            threading.Thread(target=log, daemon=True).start()
        else:
            self.alert_lbl.setText("")

    # ── Visual pulse ───────────────────────────────────────────────────────
    def _pulse(self):
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_ACCENT};
            }}
        """)
        QTimer.singleShot(2500, lambda: self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """))

    # ── Expand/collapse ────────────────────────────────────────────────────
    def toggle_expand(self):
        self.collapse() if self.is_expanded else self.expand()

    def expand(self):
        self.is_expanded = True
        self.panel.setVisible(True)
        self.container.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.expand_btn.setText("▴")

        if self.alert_log_id and self.alert_shown_at:
            secs = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(log_alert_engagement(self.alert_log_id, True, secs))
            threading.Thread(target=log, daemon=True).start()

    def collapse(self):
        self.is_expanded = False
        self.panel.setVisible(False)
        self.container.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.expand_btn.setText("▾")

    def dismiss(self):
        if self.alert_log_id and self.alert_shown_at:
            secs = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(log_alert_engagement(self.alert_log_id, False, secs))
            threading.Thread(target=log, daemon=True).start()
        self.collapse()

    def open_settings(self):
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Loom Settings")
        msg.setText(
            "Loom Bar v0.1\n\n"
            "Alert preferences are learned from your engagement.\n"
            "Engage with useful alerts — dismiss ones you don't need.\n"
            "Loom adjusts frequency automatically over time."
        )
        msg.exec()

    # ── Drag ───────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._dragging:
            p = e.globalPosition().toPoint() - self._drag_pos
            p.setY(max(0, min(p.y(), 200)))
            self.move(p)

    def mouseReleaseEvent(self, e):
        self._dragging = False

    def mouseDoubleClickEvent(self, e):
        self.toggle_expand()# surface/bar.py
#
# Updated Loom Bar with richer expanded panel:
# - Focus quality progress bar
# - Deep work time today
# - Live memory stats
# - Next compression countdown
# - Work streak
# - Active project with session count


import asyncio
import sys
import os
import threading
import json
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout,
    QVBoxLayout, QPushButton, QFrame, QSizePolicy,
    QProgressBar
)
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPen, QPixmap

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
from alerts import init_alerts_tables, get_pending_alerts, log_alert_shown, log_alert_engagement

# ── Colors ─────────────────────────────────────────────────────────────────
C_BG      = "#0a0a0b"
C_SURFACE = "#111113"
C_SURFACE2= "#18181b"
C_BORDER  = "#222226"
C_BORDER2 = "#2e2e34"
C_ACCENT  = "#c8f04a"
C_TEXT    = "#edeae4"
C_TEXT_2  = "#7a7874"
C_TEXT_3  = "#3a3835"
C_ALERT   = "#f0c44a"
C_SUCCESS = "#4af0a0"
C_DANGER  = "#ff6b6b"

# ── Dimensions ─────────────────────────────────────────────────────────────
BAR_HEIGHT_COLLAPSED = 36
BAR_HEIGHT_EXPANDED  = 320


# ── Signal bridge ──────────────────────────────────────────────────────────
class SignalBridge(QObject):
    digest_ready = pyqtSignal(object)
    alert_ready  = pyqtSignal(object)


# ── Helper: styled label ───────────────────────────────────────────────────
def make_label(text, color=None, size=11, mono=True, bold=False):
    lbl = QLabel(text)
    font_family = "'DM Mono', 'Consolas', monospace" if mono else "'DM Sans', sans-serif"
    weight = "600" if bold else "400"
    lbl.setStyleSheet(f"""
        color: {color or C_TEXT_2};
        font-family: {font_family};
        font-size: {size}px;
        font-weight: {weight};
        background: transparent;
        border: none;
    """)
    return lbl


def make_row(left_text, right_text,
             left_color=None, right_color=None,
             left_size=10, right_size=11):
    """Creates a two-column label row."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)

    left  = make_label(left_text,  left_color  or C_TEXT_3, left_size)
    left.setFixedWidth(120)
    right = make_label(right_text, right_color or C_TEXT,   right_size)
    right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    row.addWidget(left)
    row.addWidget(right)
    return row, left, right


def make_divider():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {C_BORDER}; border: none;")
    return line


# ── Loom Bar ───────────────────────────────────────────────────────────────
class LoomBar(QWidget):

    def __init__(self):
        super().__init__()
        self.bridge           = SignalBridge()
        self.digest           = None
        self.alerts           = []
        self.is_expanded      = False
        self.alert_log_id     = None
        self.alert_shown_at   = None
        self._dragging        = False
        self._drag_pos        = QPoint()

        self._setup_window()
        self._setup_ui()
        self._connect_signals()
        self._start_background_tasks()
        self._start_ui_timer()

    # ── Window ─────────────────────────────────────────────────────────────
    def _setup_window(self):
        screen = QApplication.primaryScreen().geometry()
        self.screen_width = screen.width()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)

    # ── UI ─────────────────────────────────────────────────────────────────
    def _setup_ui(self):
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Collapsed row ─────────────────────────────────────────────────
        collapsed = QFrame()
        collapsed.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        collapsed.setStyleSheet("background: transparent; border: none;")

        clo = QHBoxLayout(collapsed)
        clo.setContentsMargins(16, 0, 16, 0)
        clo.setSpacing(0)

        self.logo_lbl = QLabel("◉  L·O·O·M")
        self.logo_lbl.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            font-weight: 500;
            letter-spacing: 3px;
            padding-right: 20px;
        """)

        sep1 = QLabel("|")
        sep1.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.context_lbl = QLabel("Initializing Loom...")
        self.context_lbl.setStyleSheet(f"""
            color: {C_TEXT_2};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        """)
        self.context_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        sep2 = QLabel("|")
        sep2.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.time_lbl = QLabel()
        self.time_lbl.setStyleSheet(f"""
            color: {C_TEXT_3};
            font-family: 'DM Mono', monospace;
            font-size: 10px;
        """)

        self.expand_btn = QPushButton("▾")
        self.expand_btn.setFixedSize(28, 28)
        self.expand_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent;
                border: none; font-size: 14px; padding: 0;
            }}
            QPushButton:hover {{ color: {C_ACCENT}; }}
        """)
        self.expand_btn.clicked.connect(self.toggle_expand)

        clo.addWidget(self.logo_lbl)
        clo.addWidget(sep1)
        clo.addWidget(self.context_lbl)
        clo.addWidget(sep2)
        clo.addWidget(self.time_lbl)
        clo.addSpacing(12)
        clo.addWidget(self.expand_btn)

        # ── Expanded panel ────────────────────────────────────────────────
        self.panel = QFrame()
        self.panel.setVisible(False)
        self.panel.setStyleSheet(f"""
            QFrame {{
                background-color: {C_SURFACE};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)

        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(20, 14, 20, 14)
        pl.setSpacing(6)

        # Section: Last session
        sec1 = make_label("LAST SESSION", C_TEXT_3, 9)
        pl.addWidget(sec1)

        r1, _, self.last_session_val = make_row("Last session", "—")
        pl.addLayout(r1)

        r2, _, self.last_file_val = make_row("Last file", "—")
        pl.addLayout(r2)

        pl.addWidget(make_divider())

        # Section: Focus
        sec2 = make_label("TODAY'S FOCUS", C_TEXT_3, 9)
        pl.addWidget(sec2)

        # Focus bar row — custom layout
        focus_row = QHBoxLayout()
        focus_row.setContentsMargins(0, 0, 0, 0)
        focus_row.setSpacing(0)

        focus_key = make_label("Deep work", C_TEXT_3, 10)
        focus_key.setFixedWidth(120)

        self.focus_bar_lbl = make_label("░░░░░░░░░░", C_TEXT_3, 11)
        self.focus_bar_lbl.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 12px;
            letter-spacing: 2px;
        """)

        self.focus_time_lbl = make_label("  0m today", C_TEXT_3, 10)

        focus_row.addWidget(focus_key)
        focus_row.addWidget(self.focus_bar_lbl)
        focus_row.addWidget(self.focus_time_lbl)
        focus_row.addStretch()
        pl.addLayout(focus_row)

        r3, _, self.focus_quality_val = make_row("Quality", "—")
        pl.addLayout(r3)

        pl.addWidget(make_divider())

        # Section: Project + Memory
        sec3 = make_label("MEMORY", C_TEXT_3, 9)
        pl.addWidget(sec3)

        r4, _, self.project_val = make_row("Active project", "—")
        pl.addLayout(r4)

        r5, _, self.streak_val = make_row("Streak", "—")
        pl.addLayout(r5)

        r6, _, self.events_val = make_row("Events captured", "—")
        pl.addLayout(r6)

        r7, _, self.nodes_val = make_row("Memory nodes", "—")
        pl.addLayout(r7)

        r8, _, self.sync_val = make_row("Next compression", "—")
        pl.addLayout(r8)

        pl.addWidget(make_divider())

        # Section: Today
        sec4 = make_label("TODAY", C_TEXT_3, 9)
        pl.addWidget(sec4)

        self.today_lbl = make_label("Keep building", C_ACCENT, 11)
        pl.addWidget(self.today_lbl)

        # Alert row
        self.alert_lbl = QLabel("")
        self.alert_lbl.setStyleSheet(f"""
            color: {C_ALERT};
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            padding-top: 4px;
        """)
        self.alert_lbl.setWordWrap(True)
        pl.addWidget(self.alert_lbl)

        # Action row
        action = QHBoxLayout()
        action.setSpacing(10)
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent;
                border: 1px solid {C_BORDER}; border-radius: 5px;
                padding: 5px 14px;
                font-family: 'DM Mono', monospace; font-size: 10px;
            }}
            QPushButton:hover {{ color: {C_TEXT}; border-color: {C_TEXT_3}; }}
        """)
        self.dismiss_btn.clicked.connect(self.dismiss)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C_TEXT_3}; background: transparent; border: none;
                padding: 5px 10px;
                font-family: 'DM Mono', monospace; font-size: 10px;
            }}
            QPushButton:hover {{ color: {C_TEXT_2}; }}
        """)
        self.settings_btn.clicked.connect(self.open_settings)

        action.addWidget(self.dismiss_btn)
        action.addStretch()
        action.addWidget(self.settings_btn)
        pl.addLayout(action)

        outer.addWidget(collapsed)
        outer.addWidget(self.panel)

    # ── Signals ────────────────────────────────────────────────────────────
    def _connect_signals(self):
        self.bridge.digest_ready.connect(self._on_digest)
        self.bridge.alert_ready.connect(self._on_alerts)

    # ── Background ─────────────────────────────────────────────────────────
    def _start_background_tasks(self):
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_init())
            loop.run_until_complete(self._poll_loop())
        threading.Thread(target=run, daemon=True).start()

    async def _async_init(self):
        await init_alerts_tables()
        await self._load_digest()
        await self._load_alerts()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(60)
            await self._load_digest()
            await self._load_alerts()

    async def _load_digest(self):
        try:
            self.bridge.digest_ready.emit(await generate_digest())
        except Exception as e:
            print(f"[Bar] Digest error: {e}")

    async def _load_alerts(self):
        try:
            self.bridge.alert_ready.emit(await get_pending_alerts())
        except Exception as e:
            print(f"[Bar] Alert error: {e}")

    # ── Timer ──────────────────────────────────────────────────────────────
    def _start_ui_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30000)
        self._tick()

    def _tick(self):
        self.time_lbl.setText(datetime.now().strftime("%H:%M  %a %d %b"))

        # Update compression countdown every 30s
        if self.digest:
            elapsed = (datetime.now() - datetime.fromisoformat(
                self.digest.generated_at
            )).total_seconds() / 60
            remaining = max(0, self.digest.mins_to_next_sync - int(elapsed))
            if remaining == 0:
                self.sync_val.setText("running soon...")
            else:
                self.sync_val.setText(f"in {remaining} min")

    # ── Digest handler ─────────────────────────────────────────────────────
    def _on_digest(self, d: DigestData):
        self.digest = d

        # Collapsed bar context
        if d.current_project:
            ctx = f"{d.current_project}  ·  {d.last_session_ago}"
        elif d.last_worked_on != "Nothing recorded yet":
            ctx = f"{d.last_worked_on[:55]}  ·  {d.last_session_ago}"
        else:
            ctx = "Ready — no sessions yet"
        self.context_lbl.setText(ctx)

        # Last session
        self.last_session_val.setText(d.last_worked_on[:70] if d.last_worked_on else "—")
        self.last_file_val.setText(
            f"{d.last_file}  ·  {d.last_session_ago}" if d.last_file else d.last_session_ago or "—"
        )

        # Focus bar
        self.focus_bar_lbl.setText(d.focus_bar or "░░░░░░░░░░")
        hrs  = d.focus_minutes_today // 60
        mins = d.focus_minutes_today % 60
        if hrs > 0:
            time_str = f"  {hrs}h {mins}m today"
        else:
            time_str = f"  {mins}m today"
        self.focus_time_lbl.setText(time_str)

        # Focus quality with color
        quality = d.focus_quality or "medium"
        q_color = {
            "high":   C_SUCCESS,
            "medium": C_ACCENT,
            "low":    C_TEXT_3
        }.get(quality, C_TEXT_2)
        self.focus_quality_val.setText(quality.title())
        self.focus_quality_val.setStyleSheet(f"""
            color: {q_color};
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        """)

        # Project
        if d.current_project:
            self.project_val.setText(
                f"{d.current_project}  ·  {d.project_sessions} session(s) this week"
            )
        else:
            self.project_val.setText("No projects detected yet")

        # Streak
        if d.streak_days >= 2:
            self.streak_val.setText(f"{d.streak_days} days  🔥")
            self.streak_val.setStyleSheet(f"""
                color: {C_ACCENT};
                font-family: 'DM Mono', monospace;
                font-size: 11px;
            """)
        elif d.streak_days == 1:
            self.streak_val.setText("1 day — keep going")
        else:
            self.streak_val.setText("—")

        # Memory stats
        self.events_val.setText(f"{d.events_captured:,}")
        self.nodes_val.setText(f"{d.memory_nodes}  ·  {d.projects_detected} project(s)")

        # Compression countdown
        if d.mins_to_next_sync == 0:
            self.sync_val.setText("running soon...")
        else:
            self.sync_val.setText(f"in {d.mins_to_next_sync} min")

        # Today suggestion
        self.today_lbl.setText(d.focus_today)

    # ── Alert handler ──────────────────────────────────────────────────────
    def _on_alerts(self, alerts):
        self.alerts = alerts
        if alerts:
            a = alerts[0]
            self.alert_lbl.setText(f"{a.title}  ·  {a.body[:80]}")
            self._pulse()

            def log():
                loop = asyncio.new_event_loop()
                self.alert_log_id = loop.run_until_complete(
                    log_alert_shown(a.alert_type, a.title)
                )
                self.alert_shown_at = datetime.now()
            threading.Thread(target=log, daemon=True).start()
        else:
            self.alert_lbl.setText("")

    # ── Visual pulse ───────────────────────────────────────────────────────
    def _pulse(self):
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_ACCENT};
            }}
        """)
        QTimer.singleShot(2500, lambda: self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {C_BORDER};
            }}
        """))

    # ── Expand/collapse ────────────────────────────────────────────────────
    def toggle_expand(self):
        self.collapse() if self.is_expanded else self.expand()

    def expand(self):
        self.is_expanded = True
        self.panel.setVisible(True)
        self.container.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.setFixedHeight(BAR_HEIGHT_EXPANDED)
        self.expand_btn.setText("▴")

        if self.alert_log_id and self.alert_shown_at:
            secs = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(log_alert_engagement(self.alert_log_id, True, secs))
            threading.Thread(target=log, daemon=True).start()

    def collapse(self):
        self.is_expanded = False
        self.panel.setVisible(False)
        self.container.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        self.expand_btn.setText("▾")

    def dismiss(self):
        if self.alert_log_id and self.alert_shown_at:
            secs = (datetime.now() - self.alert_shown_at).total_seconds()
            def log():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(log_alert_engagement(self.alert_log_id, False, secs))
            threading.Thread(target=log, daemon=True).start()
        self.collapse()

    def open_settings(self):
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Loom Settings")
        msg.setText(
            "Loom Bar v0.1\n\n"
            "Alert preferences are learned from your engagement.\n"
            "Engage with useful alerts — dismiss ones you don't need.\n"
            "Loom adjusts frequency automatically over time."
        )
        msg.exec()

    # ── Drag ───────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._dragging:
            p = e.globalPosition().toPoint() - self._drag_pos
            p.setY(max(0, min(p.y(), 200)))
            self.move(p)

    def mouseReleaseEvent(self, e):
        self._dragging = False

    def mouseDoubleClickEvent(self, e):
        self.toggle_expand()