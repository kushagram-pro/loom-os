# surface/bar.py
#
# Loom Bar — ambient activity surface.
#
# SYNC MODEL:
#   - On startup: loads last known digest from DB immediately.
#   - After 60s startup delay: runs first compression + memory sync.
#   - Every 15 minutes thereafter: runs full sync (compression → embed → projects → blockers).
#   - Every 60 seconds: refreshes UI labels from DB (time, countdown, events, nodes).
#
# This file is self-contained — it does NOT need scheduler.py or query.py
# running in separate terminals. All sync happens in a background thread.

import asyncio
import sys
import os
import threading
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout,
    QVBoxLayout, QPushButton, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QFont

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE   = os.path.abspath(__file__)
_SURFACE     = os.path.dirname(_THIS_FILE)
_LOOM_ROOT   = os.path.dirname(_SURFACE)
_CAPTURE     = os.path.join(_LOOM_ROOT, 'capture')
_MEMORY      = os.path.join(_LOOM_ROOT, 'memory')
_COMPRESSION = os.path.join(_LOOM_ROOT, 'compression')

for _p in (_CAPTURE, _MEMORY, _COMPRESSION, _SURFACE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from digest import generate_digest, DigestData
from alerts import init_alerts_tables, get_pending_alerts, log_alert_shown, log_alert_engagement

# ── Colors ─────────────────────────────────────────────────────────────────
C_BG      = "#0a0a0b"
C_SURFACE = "#111113"
C_BORDER  = "#222226"
C_ACCENT  = "#c8f04a"
C_TEXT    = "#edeae4"
C_TEXT_2  = "#7a7874"
C_TEXT_3  = "#3a3835"
C_ALERT   = "#f0c44a"
C_SUCCESS = "#4af0a0"
C_DANGER  = "#ff6b6b"
C_SYNC    = "#a0c8f0"   # blue tint shown while syncing

# ── Dimensions ─────────────────────────────────────────────────────────────
BAR_HEIGHT_COLLAPSED = 36
BAR_HEIGHT_EXPANDED  = 330

# ── Sync interval ──────────────────────────────────────────────────────────
SYNC_INTERVAL_MINUTES = 15


# ── Signal bridge (thread-safe Qt signals) ─────────────────────────────────
class SignalBridge(QObject):
    digest_ready  = pyqtSignal(object)   # DigestData
    alert_ready   = pyqtSignal(object)   # list[Alert]
    sync_started  = pyqtSignal()
    sync_done     = pyqtSignal(str)      # result message
    status_line   = pyqtSignal(str)      # real-time step message


# ── Helper widgets ──────────────────────────────────────────────────────────
def make_label(text, color=None, size=11, mono=True, bold=False):
    lbl = QLabel(text)
    family = "'DM Mono', 'Consolas', monospace" if mono else "'DM Sans', sans-serif"
    weight = "600" if bold else "400"
    lbl.setStyleSheet(f"""
        color: {color or C_TEXT_2};
        font-family: {family};
        font-size: {size}px;
        font-weight: {weight};
        background: transparent;
        border: none;
    """)
    return lbl


def make_row(key_text, val_text="—", key_color=None, val_color=None):
    row  = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    key  = make_label(key_text, key_color or C_TEXT_3, 10)
    key.setFixedWidth(130)
    val  = make_label(val_text, val_color or C_TEXT, 11)
    val.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    row.addWidget(key)
    row.addWidget(val)
    return row, key, val


def make_divider():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {C_BORDER}; border: none;")
    return line


# ── Loom Bar ────────────────────────────────────────────────────────────────
class LoomBar(QWidget):

    def __init__(self):
        super().__init__()
        self.bridge         = SignalBridge()
        self.digest         = None
        self.alerts         = []
        self.is_expanded    = False
        self.alert_log_id   = None
        self.alert_shown_at = None
        self._dragging      = False
        self._drag_pos      = QPoint()
        self._is_syncing    = False
        self._last_sync_at  = None   # datetime of last completed sync

        self._setup_window()
        self._build_ui()
        self._connect_signals()
        self._start_background_thread()
        self._start_ui_timer()

    # ── Window ──────────────────────────────────────────────────────────────
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

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, self.screen_width, BAR_HEIGHT_COLLAPSED)
        self._set_border(C_BORDER)

        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Collapsed row ────────────────────────────────────────────────
        top = QFrame()
        top.setFixedHeight(BAR_HEIGHT_COLLAPSED)
        top.setStyleSheet("background: transparent; border: none;")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(16, 0, 16, 0)
        tl.setSpacing(0)

        logo = QLabel("◉  L·O·O·M")
        logo.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 11px; font-weight: 500;
            letter-spacing: 3px; padding-right: 20px;
        """)

        sep1 = QLabel("|")
        sep1.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.context_lbl = QLabel("Initializing…")
        self.context_lbl.setStyleSheet(f"""
            color: {C_TEXT_2};
            font-family: 'DM Mono', monospace; font-size: 11px;
        """)
        self.context_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        sep2 = QLabel("|")
        sep2.setStyleSheet(f"color: {C_TEXT_3}; padding: 0 12px;")

        self.time_lbl = QLabel()
        self.time_lbl.setStyleSheet(f"""
            color: {C_TEXT_3};
            font-family: 'DM Mono', monospace; font-size: 10px;
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

        tl.addWidget(logo)
        tl.addWidget(sep1)
        tl.addWidget(self.context_lbl)
        tl.addWidget(sep2)
        tl.addWidget(self.time_lbl)
        tl.addSpacing(12)
        tl.addWidget(self.expand_btn)

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

        # Section — Last session
        pl.addWidget(make_label("LAST SESSION", C_TEXT_3, 9))
        r1, _, self.last_session_val = make_row("Summary")
        pl.addLayout(r1)
        r2, _, self.last_file_val = make_row("Last file")
        pl.addLayout(r2)

        pl.addWidget(make_divider())

        # Section — Focus
        pl.addWidget(make_label("TODAY'S FOCUS", C_TEXT_3, 9))

        focus_row = QHBoxLayout()
        focus_row.setContentsMargins(0, 0, 0, 0)
        focus_row.setSpacing(0)
        focus_key = make_label("Deep work", C_TEXT_3, 10)
        focus_key.setFixedWidth(130)
        self.focus_bar_lbl = make_label("░░░░░░░░░░", C_ACCENT, 12)
        self.focus_bar_lbl.setStyleSheet(f"""
            color: {C_ACCENT};
            font-family: 'DM Mono', monospace;
            font-size: 12px; letter-spacing: 2px;
            background: transparent; border: none;
        """)
        self.focus_time_lbl = make_label("  0m today", C_TEXT_3, 10)
        focus_row.addWidget(focus_key)
        focus_row.addWidget(self.focus_bar_lbl)
        focus_row.addWidget(self.focus_time_lbl)
        focus_row.addStretch()
        pl.addLayout(focus_row)

        r3, _, self.focus_quality_val = make_row("Quality")
        pl.addLayout(r3)

        pl.addWidget(make_divider())

        # Section — Memory
        pl.addWidget(make_label("MEMORY", C_TEXT_3, 9))
        r4, _, self.project_val  = make_row("Active project")
        pl.addLayout(r4)
        r5, _, self.streak_val   = make_row("Streak")
        pl.addLayout(r5)
        r6, _, self.events_val   = make_row("Events captured")
        pl.addLayout(r6)
        r7, _, self.nodes_val    = make_row("Memory nodes")
        pl.addLayout(r7)
        r8, _, self.sync_val     = make_row("Next sync")
        pl.addLayout(r8)

        pl.addWidget(make_divider())

        # Section — Today
        pl.addWidget(make_label("TODAY", C_TEXT_3, 9))
        self.today_lbl = make_label("Keep building", C_ACCENT, 11)
        pl.addWidget(self.today_lbl)

        self.alert_lbl = QLabel("")
        self.alert_lbl.setStyleSheet(f"""
            color: {C_ALERT};
            font-family: 'DM Mono', monospace;
            font-size: 10px; padding-top: 4px;
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

        outer.addWidget(top)
        outer.addWidget(self.panel)

    def _set_border(self, color):
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {C_BG};
                border-bottom: 1px solid {color};
            }}
        """)

    # ── Signals ─────────────────────────────────────────────────────────────
    def _connect_signals(self):
        QC = Qt.ConnectionType.QueuedConnection
        self.bridge.digest_ready.connect(self._on_digest,       QC)
        self.bridge.alert_ready.connect(self._on_alerts,        QC)
        self.bridge.sync_started.connect(self._on_sync_started, QC)
        self.bridge.sync_done.connect(self._on_sync_done,       QC)
        self.bridge.status_line.connect(self._on_status_line,   QC)

    # ── Background thread ────────────────────────────────────────────────────
    def _start_background_thread(self):
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            while True:          # restart the loop if it ever crashes
                try:
                    loop.run_until_complete(self._bg_main())
                except Exception as e:
                    print(f"[Bar] Background loop crashed: {e} — restarting in 30s")
                    import time; time.sleep(30)
        threading.Thread(target=run, daemon=True, name="loom-sync").start()

    async def _bg_main(self):
        """Entry point for the background async loop."""
        print("[Bar] Background sync thread started")

        # Step 1 — init tables (each wrapped so one failure can't stop the rest)
        try:
            await init_alerts_tables()
        except Exception as e:
            print(f"[Bar] init_alerts_tables warning: {e}")

        await self._do_init_memory_tables()

        # Step 2 — load initial digest immediately so bar shows data right away
        await self._load_digest()
        await self._load_alerts()

        # Step 3 — first sync after a short startup delay (10s)
        print("[Bar] First sync in 10 seconds…")
        await asyncio.sleep(10)
        await self._run_full_sync()
        await self._load_digest()
        await self._load_alerts()

        # Step 4 — main poll loop: digest refresh every 60s, full sync every 15 min
        tick = 0
        while True:
            await asyncio.sleep(60)   # 1-minute tick
            tick += 1

            if tick % SYNC_INTERVAL_MINUTES == 0:
                await self._run_full_sync()

            await self._load_digest()
            await self._load_alerts()

    async def _do_init_memory_tables(self):
        """Init compression + memory tables so queries don't fail on first run."""
        try:
            from engine   import init_memory_table
            from projects import init_projects_table
            from blockers import init_blockers_table
            await init_memory_table()
            await init_projects_table()
            await init_blockers_table()
        except Exception as e:
            print(f"[Bar] Table init warning: {e}")

    def _emit_status(self, msg: str):
        """Thread-safe shortcut — emits a status_line signal."""
        self.bridge.status_line.emit(msg)

    async def _run_full_sync(self):
        """
        Full 15-minute sync cycle with real-time step feedback on bar:
          1. Compress raw events → memory node (via LLM)
          2. Embed new nodes → vectors in LanceDB
          3. Rebuild project clusters
          4. Detect recurring blockers
        Each step emits a status_line signal so the collapsed bar updates live.
        """
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"\n[Bar] ── Sync at {ts} ──────────────────")
        self.bridge.sync_started.emit()
        embedded = 0

        # Step 1 — compression
        self._emit_status(f"● {ts}  compressing events…")
        try:
            from engine import run_compression, init_memory_table
            await init_memory_table()
            await run_compression()
        except Exception as e:
            print(f"[Bar] Compression skipped: {e}")

        # Step 2 — embed new memory nodes
        self._emit_status(f"● {ts}  embedding memory nodes…")
        try:
            from graph import sync_graph
            embedded = await sync_graph()
            self._emit_status(f"● {ts}  {embedded} node(s) embedded")
        except Exception as e:
            print(f"[Bar] Graph sync skipped: {e}")
            embedded = 0

        # Step 3 — rebuild projects
        self._emit_status(f"● {ts}  rebuilding projects…")
        try:
            from projects import build_projects
            if embedded > 0:
                await build_projects()
        except Exception as e:
            print(f"[Bar] Project build skipped: {e}")

        # Step 4 — detect blockers
        self._emit_status(f"● {ts}  detecting blockers…")
        try:
            from blockers import detect_recurring_blockers
            await detect_recurring_blockers()
        except Exception as e:
            print(f"[Bar] Blocker detection skipped: {e}")

        result = f"synced  ·  {embedded} node(s) embedded  ·  {ts}"
        print(f"[Bar] Sync done — {embedded} node(s) embedded")
        self.bridge.sync_done.emit(result)

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

    # ── UI timer (30s tick — updates clock + sync countdown) ────────────────
    def _start_ui_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30_000)
        self._tick()

    def _tick(self):
        try:
            self.time_lbl.setText(datetime.now().strftime("%H:%M  %a %d %b"))
        except Exception:
            pass

        # Don't overwrite sync_val while actively syncing
        if self._is_syncing:
            return

        try:
            now = datetime.now()

            if self._last_sync_at is None:
                # Never synced yet since bar started
                self.sync_val.setText("first sync in <1 min…")
                self.sync_val.setStyleSheet(f"""
                    color: {C_TEXT_3};
                    font-family: 'DM Mono', monospace; font-size: 11px;
                    background: transparent; border: none;
                """)
                return

            elapsed_since_sync = (now - self._last_sync_at).total_seconds() / 60
            remaining = max(0, SYNC_INTERVAL_MINUTES - int(elapsed_since_sync))
            ago_mins  = int(elapsed_since_sync)

            if remaining == 0:
                sync_text  = "syncing soon…"
                sync_color = C_ALERT
            elif ago_mins == 0:
                sync_text  = f"in {remaining} min  ·  synced just now"
                sync_color = C_SUCCESS
            else:
                sync_text  = f"in {remaining} min  ·  last synced {ago_mins}m ago"
                sync_color = C_TEXT_2

            self.sync_val.setText(sync_text)
            self.sync_val.setStyleSheet(f"""
                color: {sync_color};
                font-family: 'DM Mono', monospace; font-size: 11px;
                background: transparent; border: none;
            """)
        except Exception:
            pass

    # ── Digest handler ───────────────────────────────────────────────────────
    def _on_digest(self, d: DigestData):
        self.digest = d

        # Collapsed bar text — always reset color to normal after sync
        self.context_lbl.setStyleSheet(f"""
            color: {C_TEXT_2};
            font-family: 'DM Mono', monospace; font-size: 11px;
        """)
        if d.current_project:
            ctx = f"{d.current_project}  ·  {d.last_session_ago}"
        elif d.last_worked_on not in ("Nothing recorded yet", ""):
            ctx = f"{d.last_worked_on[:55]}  ·  {d.last_session_ago}"
        else:
            ctx = "Ready — no sessions yet"
        self.context_lbl.setText(ctx)

        # Last session section
        self.last_session_val.setText(d.last_worked_on[:70] or "—")
        self.last_file_val.setText(
            f"{d.last_file}  ·  {d.last_session_ago}"
            if d.last_file else (d.last_session_ago or "—")
        )

        # Focus bar
        self.focus_bar_lbl.setText(d.focus_bar or "░░░░░░░░░░")
        hrs  = d.focus_minutes_today // 60
        mins = d.focus_minutes_today % 60
        self.focus_time_lbl.setText(
            f"  {hrs}h {mins}m today" if hrs > 0 else f"  {mins}m today"
        )

        quality  = d.focus_quality or "medium"
        q_colors = {"high": C_SUCCESS, "medium": C_ACCENT, "low": C_TEXT_3}
        q_color  = q_colors.get(quality, C_TEXT_2)
        self.focus_quality_val.setText(quality.title())
        self.focus_quality_val.setStyleSheet(f"""
            color: {q_color};
            font-family: 'DM Mono', monospace; font-size: 11px;
            background: transparent; border: none;
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
                font-family: 'DM Mono', monospace; font-size: 11px;
                background: transparent; border: none;
            """)
        elif d.streak_days == 1:
            self.streak_val.setText("1 day — keep going")
        else:
            self.streak_val.setText("—")

        # Memory stats
        self.events_val.setText(f"{d.events_captured:,}")
        self.nodes_val.setText(f"{d.memory_nodes}  ·  {d.projects_detected} project(s)")

        # sync_val countdown is managed by _tick (based on _last_sync_at),
        # NOT by the digest, so we don't overwrite it here.

        # Suggestion
        self.today_lbl.setText(d.focus_today or "Keep building")

    # ── Sync status handlers ─────────────────────────────────────────────────
    def _on_status_line(self, msg: str):
        """
        Called for every sync step — updates the collapsed bar context live.
        User sees each step even without expanding the panel.
        """
        self.context_lbl.setText(msg)
        self.context_lbl.setStyleSheet(f"""
            color: {C_SYNC};
            font-family: 'DM Mono', monospace; font-size: 11px;
        """)
        self.sync_val.setText(msg[:50])
        self.sync_val.setStyleSheet(f"""
            color: {C_SYNC};
            font-family: 'DM Mono', monospace; font-size: 11px;
            background: transparent; border: none;
        """)

    def _on_sync_started(self):
        self._is_syncing = True
        self._set_border(C_SYNC)

    def _on_sync_done(self, result: str):
        self._is_syncing = False
        self._last_sync_at = datetime.now()
        self._set_border(C_BORDER)
        # Show final result in collapsed bar
        self.context_lbl.setText(f"✓ {result}")
        self.context_lbl.setStyleSheet(f"""
            color: {C_SUCCESS};
            font-family: 'DM Mono', monospace; font-size: 11px;
        """)
        # Show in expanded panel
        self.sync_val.setText("just synced ✓")
        self.sync_val.setStyleSheet(f"""
            color: {C_SUCCESS};
            font-family: 'DM Mono', monospace; font-size: 11px;
            background: transparent; border: none;
        """)
        # After 6s the next _on_digest call will repopulate everything normally
        # (digest is reloaded immediately after sync_done in _bg_main)

    # ── Alert handler ────────────────────────────────────────────────────────
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

    def _pulse(self):
        self._set_border(C_ACCENT)
        QTimer.singleShot(2500, lambda: self._set_border(C_BORDER))

    # ── Expand / collapse ────────────────────────────────────────────────────
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
            "Loom Bar v0.2\n\n"
            f"Sync interval: every {SYNC_INTERVAL_MINUTES} minutes\n"
            "Compression: phi4-mini via Ollama\n\n"
            "Alert preferences are learned from your engagement.\n"
            "Engage with useful alerts — dismiss ones you don't need."
        )
        msg.exec()

    # ── Drag to reposition ───────────────────────────────────────────────────
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
