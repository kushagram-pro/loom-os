# memory/blockers.py
#
# WHAT THIS FILE DOES:
# Finds recurring obstacles across multiple work sessions.
# If the same type of blocker appears 3+ times — Loom flags it
# as a pattern worth surfacing.
#
# WHY THIS MATTERS:
# A blocker that appears once is a normal part of coding.
# A blocker that appears 5 times across different days
# is a systemic problem — bad documentation, unclear architecture,
# a dependency that keeps causing issues.
# Loom surfaces these patterns so you can address them
# rather than fighting the same battle repeatedly.
#
# HOW DETECTION WORKS:
# 1. Collect all non-null blocker fields from memory nodes
# 2. Embed each blocker as a vector
# 3. Cluster similar blockers together (cosine similarity)
# 4. Any cluster with 3+ occurrences = recurring blocker pattern
#
# STORAGE:
# Recurring blockers saved to a new SQLite table.
# Rebuilt on each graph sync run.


import json
import os
import sys
import asyncio
from datetime import datetime
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
from embeddings import embed_text

# ── Configuration ──────────────────────────────────────────────────────────
MIN_OCCURRENCES      = 3    # Minimum times a blocker appears to be "recurring"
SIMILARITY_THRESHOLD = 0.72  # How similar two blockers must be to be clustered


# ── Database setup ─────────────────────────────────────────────────────────

async def init_blockers_table():
    """
    Creates the recurring_blockers table in SQLite.

    Columns:
    - id           → unique blocker pattern ID
    - pattern      → representative description of the recurring blocker
    - occurrences  → how many times this blocker appeared
    - first_seen   → timestamp of first occurrence
    - last_seen    → timestamp of most recent occurrence
    - examples     → JSON array of example blocker texts
    - node_ids     → JSON array of memory node IDs where this appeared
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS recurring_blockers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern     TEXT    NOT NULL,
                occurrences INTEGER DEFAULT 0,
                first_seen  TEXT,
                last_seen   TEXT,
                examples    TEXT    DEFAULT "[]",
                node_ids    TEXT    DEFAULT "[]"
            )
        ''')
        await db.commit()


# ── Blocker clustering ─────────────────────────────────────────────────────

def cosine_similarity(v1: list, v2: list) -> float:
    """
    Calculates cosine similarity between two vectors.
    Returns a value between 0.0 (unrelated) and 1.0 (identical).

    We implement this manually to avoid adding numpy as a dependency.
    (Though if you already have numpy, you can use np.dot instead.)
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(v1, v2))
    magnitude1  = sum(a * a for a in v1) ** 0.5
    magnitude2  = sum(b * b for b in v2) ** 0.5

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


async def detect_recurring_blockers():
    """
    Main blocker detection function.

    Steps:
    1. Collect all non-null blockers from memory_nodes
    2. Embed each blocker text as a vector
    3. Cluster semantically similar blockers
    4. Save clusters with >= MIN_OCCURRENCES as recurring patterns

    Rebuilds the recurring_blockers table from scratch each run.
    """
    print("[Blockers] Scanning for recurring blocker patterns...")

    # Step 1: Collect all blockers from memory nodes
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            '''SELECT id, timestamp, blockers
               FROM memory_nodes
               WHERE blockers IS NOT NULL
               AND blockers != "null"
               AND blockers != ""
               ORDER BY timestamp ASC'''
        ) as cur:
            rows = await cur.fetchall()
            blocker_records = [dict(r) for r in rows]

    if not blocker_records:
        print("[Blockers] No blockers found in memory nodes")
        return 0

    print(f"[Blockers] Found {len(blocker_records)} blocker records")

    # Step 2: Embed each blocker
    embedded = []
    for record in blocker_records:
        text   = record.get("blockers", "").strip()
        vector = embed_text(text)
        if vector:
            embedded.append({
                "node_id":   record["id"],
                "timestamp": record["timestamp"],
                "text":      text,
                "vector":    vector,
            })

    if not embedded:
        print("[Blockers] No blockers could be embedded")
        return 0

    # Step 3: Cluster similar blockers
    # Greedy clustering — each blocker either joins an existing
    # cluster or starts a new one
    clusters = []  # list of lists of blocker dicts

    for blocker in embedded:
        best_cluster_idx = None
        best_similarity  = 0.0

        for i, cluster in enumerate(clusters):
            # Compare against the first blocker in the cluster
            # (representative embedding)
            representative = cluster[0]["vector"]
            sim = cosine_similarity(blocker["vector"], representative)

            if sim > best_similarity:
                best_similarity  = sim
                best_cluster_idx = i

        if best_cluster_idx is not None and best_similarity >= SIMILARITY_THRESHOLD:
            clusters[best_cluster_idx].append(blocker)
        else:
            clusters.append([blocker])

    # Step 4: Save recurring clusters
    recurring = [c for c in clusters if len(c) >= MIN_OCCURRENCES]

    print(f"[Blockers] {len(recurring)} recurring pattern(s) found")

    async with aiosqlite.connect(DB_PATH) as db:
        # Rebuild fresh
        await db.execute('DELETE FROM recurring_blockers')

        for cluster in recurring:
            # Use the most descriptive blocker text as the pattern name
            # (longest text tends to be most descriptive)
            pattern  = max(cluster, key=lambda b: len(b["text"]))["text"]
            examples = list({b["text"] for b in cluster})[:5]  # unique examples
            node_ids = [b["node_id"] for b in cluster]
            timestamps = [b["timestamp"] for b in cluster]

            await db.execute('''
                INSERT INTO recurring_blockers
                    (pattern, occurrences, first_seen, last_seen, examples, node_ids)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                pattern,
                len(cluster),
                min(timestamps),
                max(timestamps),
                json.dumps(examples),
                json.dumps(node_ids),
            ))

        await db.commit()

    return len(recurring)


async def get_recurring_blockers() -> list[dict]:
    """
    Returns all detected recurring blocker patterns.
    Ordered by occurrence count (most frequent first).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            '''SELECT * FROM recurring_blockers
               ORDER BY occurrences DESC'''
        ) as cur:
            rows = await cur.fetchall()
            blockers = []
            for r in rows:
                b = dict(r)
                for field in ["examples", "node_ids"]:
                    try:
                        b[field] = json.loads(b.get(field) or "[]")
                    except Exception:
                        b[field] = []
                blockers.append(b)
            return blockers