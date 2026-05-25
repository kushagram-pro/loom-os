# L·O·O·M
### Living Overlay Of Memory

> *The first personal fabric that adapts to you — your computer finally works for you.*

Loom is a local-first AI memory layer that runs silently on your machine. It watches how you work, compresses your activity into semantic memory nodes, builds a graph of your projects and patterns, and surfaces what you need — before you have to ask.

No cloud. No data leaving your machine. No chatbot. Just a computer that finally understands you.

---

## What it does

Every 30 minutes, Loom:

1. **Captures** everything happening on your system — every app, file, clipboard change, and focus pattern
2. **Compresses** raw events into semantic memory nodes using a local LLM (Phi-4 mini)
3. **Graphs** those nodes into projects, goals, and recurring blockers using vector embeddings
4. **Surfaces** what matters via a slim bar at the top of your screen

```
◉  L·O·O·M  |  Debugging JWT auth in auth.py  ·  1m ago  |  09:14  Mon 25 May  ▾
```

Expand the bar and see:

```
LAST SESSION
Last session    Debugging JWT token expiry in auth.py
Last file       auth.py  ·  1m ago

TODAY'S FOCUS
Deep work       ■■■■■■■□□□   2h 14m today
Quality         High

MEMORY
Active project  Loom  ·  8 sessions this week
Streak          3 days 🔥
Events captured 1,247
Memory nodes    12  ·  3 project(s)
Next compression in 18 min

TODAY
Day 3 streak — keep it going
```

---

## Architecture

Loom has four layers. Each builds on the previous.

```
┌─────────────────────────────────────────────────────┐
│  Surface Layer    Loom Bar · system tray · alerts   │
├─────────────────────────────────────────────────────┤
│  Memory Graph     LanceDB · embeddings · projects   │
├─────────────────────────────────────────────────────┤
│  Compression      Phi-4 mini · semantic nodes       │
├─────────────────────────────────────────────────────┤
│  Capture          system-wide · all apps · local    │
└─────────────────────────────────────────────────────┘
```

**Capture** — a Python service using Windows APIs to watch every app, file, clipboard event, and focus pattern system-wide. No app-specific plugins needed.

**Compression** — runs every 30 minutes. Sends batched events to Phi-4 mini via Ollama. Produces structured memory nodes: summary, intent, blockers, keywords, focus quality.

**Memory graph** — embeds every node using `nomic-embed-text`. Stores vectors in LanceDB. Clusters nodes into projects. Detects recurring blockers. All local.

**Surface** — a PyQt6 bar pinned to the top of your screen. Shows live context, focus stats, memory health, and alerts. Learns what you engage with and adjusts over time.

---

## Requirements

- Windows 11
- Python 3.10+
- 16GB RAM recommended
- [Ollama](https://ollama.com) installed and running

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/loom.git
cd loom
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Windows long path issue?** If pip fails with a path length error, run this in PowerShell as Administrator then restart:
> ```powershell
> New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
> ```

### 3. Install and start Ollama

Download from [ollama.com](https://ollama.com), install, then pull the required models:

```bash
ollama pull phi4-mini
ollama pull nomic-embed-text
```

Verify both are available:

```bash
ollama list
```

### 4. Configure your project folders

Open `capture/watchers/vscode.py` and edit `WATCH_PATHS` to point at your project directories:

```python
WATCH_PATHS = [
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Projects"),
    # Add your paths here
]
```

### 5. Enable Windows long paths (if not already done)

Required for PyQt6 to install correctly. Run in PowerShell as Administrator:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

Restart your machine.

---

## Running Loom

One command starts everything:

```bash
cd loom
python surface/surface.py
```

This launches all four layers in sequence:

```
[Launcher] ✓ Capture started       — watching all applications
[Launcher] ✓ Compression started   — compressing every 30 minutes
[Launcher] ✓ Memory Graph started  — building semantic memory
[Launcher] Bar appears in 15 seconds...
```

On first run, Loom registers itself in Windows startup so it launches automatically on every login.

To stop: right-click the tray icon → **Quit Loom**

---

## Project structure

```
loom/
├── capture/                  Layer 1 — event capture
│   ├── main.py               entry point
│   ├── database.py           SQLite interface
│   ├── processor.py          event filter + save
│   ├── attention_filter.py   drops noise, scores importance
│   └── watchers/
│       ├── system_events.py  all apps via Windows hooks
│       ├── screen_context.py UI content via accessibility API
│       ├── activity_rhythm.py keyboard/mouse focus patterns
│       ├── clipboard.py      clipboard content changes
│       └── vscode.py         file saves in project folders
│
├── compression/              Layer 2 — semantic compression
│   ├── scheduler.py          runs every 30 minutes
│   ├── engine.py             core compression logic
│   ├── prompt.py             prompt builder + response parser
│   └── debug_compression.py  inspect memory nodes
│
├── memory/                   Layer 3 — memory graph
│   ├── query.py              entry point + all queries
│   ├── graph.py              LanceDB vector store
│   ├── embeddings.py         nomic-embed-text via Ollama
│   ├── projects.py           project detection + staleness
│   └── blockers.py           recurring blocker detection
│
├── surface/                  Layer 4 — ambient UI
│   ├── surface.py            master launcher (run this)
│   ├── bar.py                Loom Bar — PyQt6 overlay
│   ├── tray.py               system tray icon + menu
│   ├── digest.py             morning summary generator
│   └── alerts.py             smart alerts + behavior learning
│
├── data/                     local storage (auto-created)
│   ├── loom_events.db        SQLite — events + memory nodes
│   └── loom_vectors/         LanceDB — vector embeddings
│
└── requirements.txt
```

---

## Debugging

**Check what's been captured:**

```bash
python check.py
```

**Inspect memory nodes:**

```bash
python compression/debug_compression.py          # last 5 nodes
python compression/debug_compression.py 20       # last 20 nodes
python compression/debug_compression.py full     # full detail
python compression/debug_compression.py stats    # breakdown
python compression/debug_compression.py now      # run immediately
```

**Query the memory graph:**

```bash
python memory/query.py week                      # this week's work
python memory/query.py stale                     # inactive projects
python memory/query.py blockers                  # recurring obstacles
python memory/query.py search "JWT auth"         # semantic search
python memory/query.py stats                     # graph health
python memory/query.py sync                      # manual sync
```

---

## Privacy

Everything runs locally on your machine.

- No data is sent to any server
- No cloud API calls for core functionality
- All models run via Ollama on your hardware
- SQLite database stored at `data/loom_events.db` — yours entirely
- No telemetry, no analytics, no ad targeting — ever

Loom watches your screen activity to build memory. It never records keystrokes, passwords, or sensitive form fields. UI Automation skips password fields explicitly. Clipboard content is capped at 1000 characters.

---

## Dependencies

```
pywin32          Windows system APIs
pywinauto        Windows UI automation
uiautomation     accessibility tree reading
pynput           keyboard/mouse activity (timing only, not content)
pyperclip        clipboard monitoring
watchdog         file system events
aiosqlite        async SQLite
psutil           process information
ollama           local LLM runtime
lancedb          local vector database
pyarrow          LanceDB schema support
pandas           data processing
PyQt6            UI framework for Loom Bar
```

Install all:

```bash
pip install -r requirements.txt
```

---

## How memory nodes look

After a 30-minute work session, the compression engine produces:

```json
{
  "summary":       "Debugging JWT token expiry in auth.py",
  "intent":        "Fix silent authentication failures in production",
  "blockers":      "Token refresh interaction with middleware unclear",
  "apps_used":     ["Code", "Chrome"],
  "files_touched": ["auth.py", "middleware.py"],
  "focus_quality": "high",
  "session_type":  "debugging",
  "keywords":      ["JWT", "authentication", "token", "middleware"]
}
```

These nodes accumulate over time, get embedded as vectors, and cluster into projects automatically.

---

## Roadmap

| Version | Focus |
|---------|-------|
| v0.1 | ✓ All four layers working — capture, compress, graph, surface |
| v0.2 | Stable across multiple machines · bug fixes from beta users |
| v0.3 | Feedback loop · fine-tuned compression model on real data |
| v0.4 | Polished UI · easier installation · faster compression |
| v1.0 | Public launch · onboarding flow · documentation |

---

## Contributing

Loom is early stage. If you're a developer who loses context switching between projects — this was built for you.

Ways to contribute:

- **Test it** on your machine and open issues for anything that breaks
- **Improve the prompt** in `compression/prompt.py` — better prompts = better memory nodes
- **Add watchers** in `capture/watchers/` for new data sources
- **Improve project detection** in `memory/projects.py`
- **Design the surface** — the bar is intentionally minimal, there's room for creativity

Open an issue before opening a PR so we can discuss the direction.

---

## License

MIT — do what you want, just don't sell people's memory data.

---

## Built by

Kushagra — solo founder building the memory layer your OS was always missing.

*"The first personal fabric that adapts to you."*

---

<div align="center">
  <sub>L · O · O · M — Living Overlay Of Memory</sub>
</div>
