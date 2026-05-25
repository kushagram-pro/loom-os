# memory/graph.py
#
# WHAT THIS FILE DOES:
# The core of layer 3. Reads memory nodes from SQLite,
# embeds them as vectors, stores them in LanceDB, and
# provides functions to search and connect them.
#
# TWO STORAGE SYSTEMS WORKING TOGETHER:
#
#   SQLite (loom_events.db)
#   └── memory_nodes table     ← structured data, metadata, text fields
#
#   LanceDB (loom_vectors/)
#   └── memory_vectors table   ← vector embeddings for similarity search
#
# Every memory node exists in both. SQLite holds the meaning.
# LanceDB holds the vector. They're linked by node_id.
#
# THE GRAPH CONCEPT:
# We don't store explicit graph edges in a separate structure.
# Instead the graph is implicit — nodes are connected by:
#   1. Vector similarity   → similar topics cluster together
#   2. Time proximity      → nearby sessions are related
#   3. Shared keywords     → same technical terms = same project
#   4. Shared files        → same files = same codebase area
#
# This approach is simpler to build and query than a traditional
# graph database, while still capturing all the connections we need.


import json
import os
import sys
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import lancedb
import pyarrow as pa

# ── Path setup ─────────────────────────────────────────────────────────────
_THIS_FILE  = os.path.abspath(__file__)
_MEMORY     = os.path.dirname(_THIS_FILE)
_LOOM_ROOT  = os.path.dirname(_MEMORY)
_CAPTURE    = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)

from database import DB_PATH
from embeddings import embed_node, embed_query, EMBED_DIMENSION

# ── LanceDB path ───────────────────────────────────────────────────────────
VECTORS_PATH  = os.path.join(_LOOM_ROOT, 'data', 'loom_vectors')
VECTORS_TABLE = "memory_vectors"


# ── LanceDB schema ─────────────────────────────────────────────────────────
# Defines the structure of each row in the vector store.
# PyArrow schema required by LanceDB.

def get_vector_schema() -> pa.Schema:
    return pa.schema([
        pa.field("node_id",       pa.int64()),
        pa.field("timestamp",     pa.string()),
        pa.field("summary",       pa.string()),
        pa.field("session_type",  pa.string()),
        pa.field("focus_quality", pa.string()),
        pa.field("keywords",      pa.string()),   # JSON string
        pa.field("files_touched", pa.string()),   # JSON string
        pa.field("vector",        pa.list_(pa.float32(), EMBED_DIMENSION)),
    ])


# ── LanceDB connection ─────────────────────────────────────────────────────

def get_vector_db():
    """
    Opens or creates the LanceDB vector store.
    LanceDB stores data as files in the loom_vectors/ folder.
    No server needed — it's fully embedded like SQLite.
    """
    os.makedirs(VECTORS_PATH, exist_ok=True)
    return lancedb.connect(VECTORS_PATH)


def get_or_create_table(db):
    """
    Gets the memory_vectors table from LanceDB.
    Creates it with the correct schema if it doesn't exist yet.
    """
    existing = db.table_names()
    if VECTORS_TABLE in existing:
        return db.open_table(VECTORS_TABLE)
    else:
        # Create empty table with our schema
        schema = get_vector_schema()
        return db.create_table(VECTORS_TABLE, schema=schema)


# ── Core graph operations ──────────────────────────────────────────────────

async def get_unembedded_nodes() -> list[dict]:
    """
    Fetches memory nodes from SQLite that haven't been
    embedded into the vector store yet.

    How we track what's been embedded:
    We check which node IDs exist in LanceDB and fetch
    only the SQLite nodes whose IDs aren't there yet.
    """
    # Get IDs already in vector store
    try:
        db    = get_vector_db()
        table = get_or_create_table(db)
        existing_df    = table.to_pandas()
        embedded_ids   = set(existing_df["node_id"].tolist()) if len(existing_df) > 0 else set()
    except Exception:
        embedded_ids = set()

    # Fetch all memory nodes from SQLite
    async with aiosqlite.connect(DB_PATH) as sqlite:
        sqlite.row_factory = aiosqlite.Row
        async with sqlite.execute(
            'SELECT * FROM memory_nodes ORDER BY timestamp ASC'
        ) as cur:
            rows = await cur.fetchall()
            all_nodes = [dict(r) for r in rows]

    # Return only nodes not yet embedded
    unembedded = [
        n for n in all_nodes
        if n["id"] not in embedded_ids
    ]

    return unembedded


async def embed_and_store_node(node: dict) -> bool:
    """
    Generates an embedding for one memory node and stores
    it in LanceDB alongside its metadata.

    Returns True if successful, False if embedding failed.
    """
    # Deserialise JSON fields from SQLite storage
    for field in ["keywords", "files_touched", "apps_used"]:
        val = node.get(field)
        if isinstance(val, str):
            try:
                node[field] = json.loads(val)
            except Exception:
                node[field] = []

    # Generate the vector embedding
    vector = embed_node(node)
    if vector is None:
        return False

    # Prepare the row for LanceDB
    row = {
        "node_id":       node["id"],
        "timestamp":     node.get("timestamp", ""),
        "summary":       node.get("summary") or "",
        "session_type":  node.get("session_type") or "mixed",
        "focus_quality": node.get("focus_quality") or "medium",
        "keywords":      json.dumps(node.get("keywords", [])),
        "files_touched": json.dumps(node.get("files_touched", [])),
        "vector":        [float(v) for v in vector],
    }

    # Store in LanceDB
    try:
        db    = get_vector_db()
        table = get_or_create_table(db)
        table.add([row])
        return True
    except Exception as e:
        print(f"[Graph] Failed to store vector: {e}")
        return False


async def sync_graph():
    """
    Main sync function. Finds all unembedded memory nodes
    and adds them to the vector store.

    Call this:
    - On startup to catch up on any missed nodes
    - After each compression run to embed new nodes immediately
    - On a schedule (runs alongside the compression scheduler)

    This is the function that builds the graph over time.
    """
    unembedded = await get_unembedded_nodes()

    if not unembedded:
        print("[Graph] All nodes embedded — graph is up to date")
        return 0

    print(f"[Graph] Embedding {len(unembedded)} new memory node(s)...")

    success_count = 0
    for node in unembedded:
        node_id  = node.get("id")
        summary  = (node.get("summary") or "")[:60]
        ok       = await embed_and_store_node(node)

        if ok:
            success_count += 1
            print(f"  ✓ Node {node_id}: {summary}")
        else:
            print(f"  ✗ Node {node_id}: embedding failed")

    print(f"[Graph] Sync complete — {success_count}/{len(unembedded)} embedded")
    return success_count


# ── Similarity search ──────────────────────────────────────────────────────

def find_similar_nodes(
    query_text: str,
    limit: int = 5,
    min_score: float = 0.6
) -> list[dict]:
    """
    Finds memory nodes semantically similar to a query string.
    Uses cosine similarity in the vector space.

    Example:
        results = find_similar_nodes("JWT authentication problems")
        # Returns nodes about auth bugs even if they use different words

    Parameters:
        query_text  → natural language search query
        limit       → max number of results to return
        min_score   → minimum similarity score (0.0 to 1.0)
                      0.6 = moderately similar
                      0.8 = very similar
                      1.0 = identical meaning

    Returns list of node dicts with added "_score" field.
    """
    vector = embed_query(query_text)
    if vector is None:
        return []

    try:
        db    = get_vector_db()
        table = get_or_create_table(db)

        # Check if table has any data
        if len(table.to_pandas()) == 0:
            return []

        # Perform vector similarity search
        results = (
            table.search(vector)
                 .limit(limit)
                 .to_pandas()
        )

        nodes = []
        for _, row in results.iterrows():
            score = float(row.get("_distance", 1.0))
            # LanceDB returns L2 distance — convert to similarity score
            # Lower distance = higher similarity
            similarity = max(0.0, 1.0 - (score / 2.0))

            if similarity >= min_score:
                node = {
                    "node_id":       int(row["node_id"]),
                    "timestamp":     row["timestamp"],
                    "summary":       row["summary"],
                    "session_type":  row["session_type"],
                    "focus_quality": row["focus_quality"],
                    "keywords":      json.loads(row.get("keywords") or "[]"),
                    "files_touched": json.loads(row.get("files_touched") or "[]"),
                    "_score":        round(similarity, 3),
                }
                nodes.append(node)

        return nodes

    except Exception as e:
        print(f"[Graph] Search failed: {e}")
        return []


def find_nodes_by_timerange(
    days_back: int = 7,
    limit: int = 50
) -> list[dict]:
    """
    Returns memory nodes from the last N days.
    Used for "what have I been working on this week" queries.
    Combines time filtering with metadata from LanceDB.
    """
    try:
        db    = get_vector_db()
        table = get_or_create_table(db)
        df    = table.to_pandas()

        if len(df) == 0:
            return []

        cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()

        # Filter by timestamp
        recent = df[df["timestamp"] >= cutoff].copy()
        recent = recent.sort_values("timestamp", ascending=False).head(limit)

        nodes = []
        for _, row in recent.iterrows():
            nodes.append({
                "node_id":       int(row["node_id"]),
                "timestamp":     row["timestamp"],
                "summary":       row["summary"],
                "session_type":  row["session_type"],
                "focus_quality": row["focus_quality"],
                "keywords":      json.loads(row.get("keywords") or "[]"),
                "files_touched": json.loads(row.get("files_touched") or "[]"),
            })

        return nodes

    except Exception as e:
        print(f"[Graph] Time range query failed: {e}")
        return []


async def get_graph_stats() -> dict:
    """
    Returns statistics about the current state of the memory graph.
    Used by query.py and debug tools.
    """
    try:
        db    = get_vector_db()
        table = get_or_create_table(db)
        df    = table.to_pandas()
        total_vectors = len(df)
    except Exception:
        total_vectors = 0

    async with aiosqlite.connect(DB_PATH) as sqlite:
        async with sqlite.execute(
            'SELECT COUNT(*) FROM memory_nodes'
        ) as cur:
            total_nodes = (await cur.fetchone())[0]

    return {
        "total_memory_nodes": total_nodes,
        "total_embedded":     total_vectors,
        "unembedded":         total_nodes - total_vectors,
    }