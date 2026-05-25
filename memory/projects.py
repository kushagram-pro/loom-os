# memory/projects.py
#
# WHAT THIS FILE DOES:
# Detects which project each memory node belongs to,
# groups nodes into project clusters, and tracks
# project activity over time.
#
# HOW PROJECT DETECTION WORKS:
# We don't ask the user to name their projects.
# We infer them automatically from three signals:
#
#   1. File paths   → nodes touching the same folder = same project
#   2. Keywords     → semantically similar keywords = same project
#   3. Vector similarity → nodes close in vector space = same project
#
# Projects are stored in SQLite as a new table.
# Each project has a name (inferred), list of related node IDs,
# last active timestamp, and activity count.
#
# STALE PROJECT DETECTION:
# A project is "stale" if its most recent node is older than
# STALE_DAYS. These are surfaced by the query layer as alerts.


import json
import os
import sys
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

import aiosqlite

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE = os.path.abspath(__file__)
_MEMORY    = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_MEMORY)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)

from database import DB_PATH
from graph import find_similar_nodes

# ── Configuration ──────────────────────────────────────────────────────────
STALE_DAYS             = 5     # Days without activity = stale project
MIN_SESSIONS_TO_TRACK  = 2     # Need at least 2 sessions to form a project
SIMILARITY_THRESHOLD   = 0.65  # Minimum similarity to group nodes together


# ── Database setup ─────────────────────────────────────────────────────────

async def init_projects_table():
    """
    Creates the projects table in SQLite.
    Safe to call on every startup.

    Columns:
    - id            → unique project ID
    - name          → inferred project name
    - node_ids      → JSON array of related memory node IDs
    - first_seen    → timestamp of first related node
    - last_active   → timestamp of most recent related node
    - session_count → how many memory nodes belong to this project
    - is_stale      → 1 if no activity in STALE_DAYS days
    - keywords      → JSON array of common keywords across sessions
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                node_ids      TEXT    DEFAULT "[]",
                first_seen    TEXT,
                last_active   TEXT,
                session_count INTEGER DEFAULT 0,
                is_stale      INTEGER DEFAULT 0,
                keywords      TEXT    DEFAULT "[]"
            )
        ''')
        await db.commit()


# ── Project inference ──────────────────────────────────────────────────────

def infer_project_name(nodes: list[dict]) -> str:
    """
    Infers a project name from a cluster of related memory nodes.

    Strategy:
    1. Look for common file path components (folder name = project name)
    2. Look for the most frequent keyword across nodes
    3. Use the summary of the most recent node as fallback

    Returns a clean, readable project name.
    """
    # Strategy 1: Common folder name from file paths
    folder_counts = defaultdict(int)
    for node in nodes:
        files = node.get("files_touched", [])
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except Exception:
                files = []

        for filepath in files:
            # Normalise path separators
            parts = filepath.replace("\\", "/").split("/")
            # Look at parent folders (not the filename itself)
            for part in parts[:-1]:
                if part and part not in {
                    "Users", "Desktop", "Documents",
                    "Projects", "dev", "code", "src",
                    "loom", "compression", "capture", "memory"
                }:
                    folder_counts[part] += 1

    if folder_counts:
        best_folder = max(folder_counts, key=folder_counts.get)
        if folder_counts[best_folder] >= 2:
            return best_folder.replace("-", " ").replace("_", " ").title()

    # Strategy 2: Most frequent keyword
    keyword_counts = defaultdict(int)
    for node in nodes:
        kw = node.get("keywords", [])
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = []
        for k in kw:
            if k and len(k) > 3:
                keyword_counts[k.lower()] += 1

    if keyword_counts:
        best_kw = max(keyword_counts, key=keyword_counts.get)
        if keyword_counts[best_kw] >= 2:
            return best_kw.title()

    # Strategy 3: Summary of most recent node
    recent = sorted(nodes, key=lambda n: n.get("timestamp", ""), reverse=True)
    if recent:
        summary = recent[0].get("summary") or ""
        # Take first 6 words of summary as project name
        words = summary.split()[:6]
        if words:
            return " ".join(words)

    return "Unnamed Project"


def collect_project_keywords(nodes: list[dict]) -> list[str]:
    """
    Collects and deduplicates keywords across all nodes in a project.
    Returns the top 10 most common keywords.
    """
    keyword_counts = defaultdict(int)
    for node in nodes:
        kw = node.get("keywords", [])
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = []
        for k in kw:
            if k:
                keyword_counts[k.lower()] += 1

    # Sort by frequency, return top 10
    sorted_kw = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return [k for k, _ in sorted_kw[:10]]


# ── Project building ───────────────────────────────────────────────────────

async def build_projects():
    """
    Main project detection function. Reads all memory nodes,
    clusters them into projects, and saves to the projects table.

    Algorithm:
    1. Fetch all memory nodes ordered by time
    2. For each node, check if it's similar to an existing project
    3. If similar → add to that project
    4. If not similar → start a new project
    5. Save all projects with session counts >= MIN_SESSIONS_TO_TRACK

    This runs after sync_graph() has embedded all nodes.
    """
    print("[Projects] Building project clusters...")

    # Fetch all nodes from SQLite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM memory_nodes ORDER BY timestamp ASC'
        ) as cur:
            rows = await cur.fetchall()
            all_nodes = [dict(r) for r in rows]

    if not all_nodes:
        print("[Projects] No memory nodes yet")
        return

    # Cluster nodes into projects using keyword overlap
    # Simple greedy clustering — fast and good enough for v0
    clusters = []  # list of lists of nodes

    for node in all_nodes:
        # Get this node's keywords
        kw = node.get("keywords", [])
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = []
        node_kw = set(k.lower() for k in kw if k)

        # Get this node's files
        files = node.get("files_touched", [])
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except Exception:
                files = []
        node_files = set(
            f.replace("\\", "/").split("/")[-1]
            for f in files
        )

        # Try to find a matching cluster
        best_cluster_idx = None
        best_overlap     = 0

        for i, cluster in enumerate(clusters):
            # Collect keywords and files from the cluster
            cluster_kw    = set()
            cluster_files = set()

            for cn in cluster:
                ckw = cn.get("keywords", [])
                if isinstance(ckw, str):
                    try:
                        ckw = json.loads(ckw)
                    except Exception:
                        ckw = []
                cluster_kw.update(k.lower() for k in ckw if k)

                cf = cn.get("files_touched", [])
                if isinstance(cf, str):
                    try:
                        cf = json.loads(cf)
                    except Exception:
                        cf = []
                cluster_files.update(
                    f.replace("\\", "/").split("/")[-1]
                    for f in cf
                )

            # Calculate overlap
            kw_overlap   = len(node_kw & cluster_kw)
            file_overlap = len(node_files & cluster_files)
            total_overlap = kw_overlap * 2 + file_overlap * 3  # files weighted more

            if total_overlap > best_overlap:
                best_overlap     = total_overlap
                best_cluster_idx = i

        # Add to best matching cluster or start a new one
        if best_cluster_idx is not None and best_overlap >= 2:
            clusters[best_cluster_idx].append(node)
        else:
            clusters.append([node])

    # Save projects with enough sessions
    saved = 0
    async with aiosqlite.connect(DB_PATH) as db:
        # Clear old project data and rebuild fresh
        await db.execute('DELETE FROM projects')

        for cluster in clusters:
            if len(cluster) < MIN_SESSIONS_TO_TRACK:
                continue

            name          = infer_project_name(cluster)
            node_ids      = [n["id"] for n in cluster]
            timestamps    = [n.get("timestamp", "") for n in cluster]
            first_seen    = min(timestamps)
            last_active   = max(timestamps)
            session_count = len(cluster)
            keywords      = collect_project_keywords(cluster)

            # Check if stale
            last_dt   = datetime.fromisoformat(last_active)
            is_stale  = 1 if (datetime.now() - last_dt).days >= STALE_DAYS else 0

            await db.execute('''
                INSERT INTO projects
                    (name, node_ids, first_seen, last_active,
                     session_count, is_stale, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                name,
                json.dumps(node_ids),
                first_seen,
                last_active,
                session_count,
                is_stale,
                json.dumps(keywords),
            ))
            saved += 1

        await db.commit()

    print(f"[Projects] {saved} project(s) detected and saved")
    return saved


async def get_all_projects() -> list[dict]:
    """
    Returns all tracked projects from the database.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM projects ORDER BY last_active DESC'
        ) as cur:
            rows = await cur.fetchall()
            projects = []
            for r in rows:
                p = dict(r)
                for field in ["node_ids", "keywords"]:
                    try:
                        p[field] = json.loads(p.get(field) or "[]")
                    except Exception:
                        p[field] = []
                projects.append(p)
            return projects


async def get_stale_projects() -> list[dict]:
    """
    Returns projects with no activity in STALE_DAYS days.
    Used by the surface layer to generate stale project alerts.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM projects WHERE is_stale = 1 ORDER BY last_active ASC'
        ) as cur:
            rows = await cur.fetchall()
            projects = []
            for r in rows:
                p = dict(r)
                for field in ["node_ids", "keywords"]:
                    try:
                        p[field] = json.loads(p.get(field) or "[]")
                    except Exception:
                        p[field] = []
                projects.append(p)
            return projects