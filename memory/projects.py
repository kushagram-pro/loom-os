# memory/projects.py
#
# Project detection — v3
#
# THE CORE PROBLEM WITH v2:
#   "Coding Loom in VS Code" and "Writing Loom pitch in Word" have DIFFERENT
#   vector embeddings. The embedding model sees tool-level activity, not the
#   underlying project. So a 0.45 cosine threshold broke them apart.
#
# HOW v3 FIXES IT — five signals, not three:
#
#   A. Vector cosine        (weight 0.38) — semantic similarity, already good
#   B. Keyword Jaccard      (weight 0.15) — LLM keyword overlap
#   C. Summary word overlap (weight 0.05) — small text signal
#   D. Temporal proximity   (weight 0.17) — same-day context switches
#      (VS Code at 2pm → Chrome/Stack Overflow at 2:30pm → Claude at 3pm
#       are almost certainly the same project session)
#   E. Project fingerprint  (weight 0.25) — project-name identification
#      Extracts specific tokens like "loom", "financeapp" from:
#        • non-generic folder names in file paths
#        • keywords confirmed by proper-noun presence in summaries
#        • intent field project references
#      A shared fingerprint token means "same project" with high confidence.
#
# THRESHOLD: 0.30 (lower than v2's 0.45; safe because average-linkage
#                  prevents chain-linking)
#
# RESULT:
#   VS Code + Word + Chrome + Claude sessions all merge into one project
#   when they share a project fingerprint token like "loom".
#   Music, unrelated browsing stay separate (no shared fingerprint + low vec).


import json
import os
import sys
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

import aiosqlite
import numpy as np

_THIS_FILE = os.path.abspath(__file__)
_MEMORY    = os.path.dirname(_THIS_FILE)
_LOOM_ROOT = os.path.dirname(_MEMORY)
_CAPTURE   = os.path.join(_LOOM_ROOT, 'capture')

sys.path.insert(0, _CAPTURE)
sys.path.insert(0, _MEMORY)

from database import DB_PATH

# ── Configuration ──────────────────────────────────────────────────────────
STALE_DAYS            = 7     # days without activity = stale
MIN_SESSIONS          = 1     # even a single-session project is worth tracking
VECTOR_SIM_THRESHOLD  = 0.30  # combined similarity to merge two nodes (v3: lower)
KEYWORD_SIM_THRESHOLD = 0.10
SUMMARY_SHARED_MIN    = 3

# Truly generic path components — not project names
GENERIC_PATH_PARTS = {
    "users", "desktop", "documents", "downloads", "appdata",
    "local", "roaming", "programdata", "program files",
    "windows", "system32", "temp", "dev", "code", "repos",
    "github", "src", "projects", "home", "onedrive",
}

# Common tools / languages / platforms — these appear in ALL projects,
# so they don't identify a specific project.
GENERIC_TECH_WORDS = {
    # Languages / formats
    "python", "javascript", "typescript", "react", "node", "html", "css",
    "json", "yaml", "toml", "bash", "shell", "rust", "golang", "java",
    # Data / infra
    "sqlite", "database", "server", "client", "frontend", "backend",
    "local", "remote", "cloud", "storage", "cache", "queue",
    # Browsers / OS
    "chrome", "firefox", "edge", "safari", "browser", "windows", "macos",
    "linux", "android", "iphone",
    # Big tech / services
    "github", "google", "microsoft", "office", "apple", "amazon", "azure",
    "chatgpt", "claude", "ollama", "openai", "anthropic", "gemini",
    "youtube", "spotify", "discord", "twitter", "linkedin", "reddit",
    "slack", "notion", "obsidian", "medium", "stack", "overflow",
    # Dev tools
    "vscode", "cursor", "visual", "studio", "jetbrains", "notepad",
    "excel", "word", "powerpoint", "outlook", "teams", "zoom",
    # Generic dev concepts (appear in every project)
    "coding", "debugging", "testing", "fixing", "error", "function",
    "class", "object", "method", "import", "install", "setup", "config",
    "terminal", "command", "script", "program", "application", "module",
    "library", "package", "file", "folder", "directory", "path",
    "index", "main", "utils", "helper", "manager", "handler",
    # Generic AI/LLM output words (appear whenever you use an AI tool)
    "generation", "generated", "generating", "response", "request",
    "dialogue", "conversation", "prompt", "output", "input", "result",
    "model", "training", "inference", "embedding", "token",
    # Generic UI/UX words (appear in any app with a UI)
    "interface", "elements", "component", "widget", "window", "panel",
    "button", "modal", "sidebar", "toolbar", "layout", "styling", "theme",
    # Generic process words
    "processing", "analysis", "management", "implementation", "interaction",
    "integration", "navigation", "configuration", "documentation",
    "streaming", "monitoring", "logging", "tracking",
    # Generic document words
    "document", "editing", "review", "draft", "version", "update",
    "content", "section", "paragraph", "summary", "notes",
}

# Stop-words for summary-based name extraction
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "by", "from", "is", "was", "are", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "this", "that", "these", "those",
    "i", "we", "you", "he", "she", "it", "they", "my", "our", "its",
    "work", "worked", "working", "session", "using", "used", "through",
    "file", "files", "folder", "edited", "editing", "related", "reading",
    "writing", "navigating", "interacting", "participating", "ran", "run",
    "new", "old", "via", "into", "some", "more", "about", "within",
    "also", "then", "when", "while", "after", "before", "during",
    "reviewing", "checked", "opened", "viewed", "watched", "searched",
}


# ── Database setup ─────────────────────────────────────────────────────────

async def init_projects_table():
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


# ── Vector helpers ─────────────────────────────────────────────────────────

def _load_vectors() -> dict[int, np.ndarray]:
    """Load all node vectors from LanceDB keyed by node_id."""
    try:
        from graph import get_vector_db, get_or_create_table
        db    = get_vector_db()
        table = get_or_create_table(db)
        df    = table.to_pandas()
        if len(df) == 0:
            return {}
        return {
            int(row["node_id"]): np.array(row["vector"], dtype=np.float32)
            for _, row in df.iterrows()
        }
    except Exception as e:
        print(f"[Projects] Could not load vectors: {e}")
        return {}


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


# ── Parsing helpers ────────────────────────────────────────────────────────

def _parse_list(val) -> list:
    if isinstance(val, list):
        return val
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


def _node_keyword_set(node: dict) -> set[str]:
    kw     = _parse_list(node.get("keywords"))
    tokens = set()
    for k in kw:
        for word in k.lower().split():
            if len(word) > 3 and word not in STOPWORDS:
                tokens.add(word)
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union > 0 else 0.0


def _summary_words(node: dict) -> set[str]:
    """Meaningful words from a node's summary."""
    summary = (node.get("summary") or "").lower()
    words   = set()
    for word in summary.split():
        word = word.strip(".,!?;:'\"()[]")
        if len(word) > 3 and word not in STOPWORDS:
            words.add(word)
    return words


# ── Signal D: Temporal proximity ────────────────────────────────────────────
# Sessions close in time are likely on the same project (tool-switching).
# e.g. VS Code → Chrome/Stack Overflow → Claude → back to VS Code.

def _temporal_sim(ni: dict, nj: dict) -> float:
    """
    Returns a similarity boost based on time between two sessions.
    Same-hour sessions almost always share a project context.
    """
    try:
        ti    = datetime.fromisoformat(ni.get("timestamp", ""))
        tj    = datetime.fromisoformat(nj.get("timestamp", ""))
        hours = abs((ti - tj).total_seconds()) / 3600

        if hours < 1:    return 0.40   # same hour — very likely same project
        elif hours < 3:  return 0.30   # same work block
        elif hours < 8:  return 0.20   # same day, different block
        elif hours < 24: return 0.10   # same day
        elif hours < 72: return 0.05   # within 3 days (returned to project)
        else:            return 0.0
    except Exception:
        return 0.0


# ── Signal E: Project fingerprint ───────────────────────────────────────────
# Extracts project-specific identifiers that survive tool changes.
# "Loom" in a VS Code session + "Loom" in a Word session → same project.
# Generic tech words ("python", "chrome") are excluded — they appear in all projects.

# URL schemes that pollute files_touched — skip folder extraction for these
_URL_PREFIXES = ("http://", "https://", "vscode-file://", "ftp://")

def _is_hex_noise(s: str) -> bool:
    """True for UUID fragments, hex hashes, and other meaningless hex strings."""
    hex_chars = set("0123456789abcdef")
    # 4+ char all-hex strings are UUIDs / git hashes / memory addresses, not words
    return len(s) >= 4 and all(c in hex_chars for c in s)


def _tokenise_name(raw: str) -> list[str]:
    """
    Split a filename stem, folder name, or title into meaningful tokens.
    Handles: underscores, hyphens, spaces, VS Code title format ("a - b - c").
    Returns lowercase tokens that are long enough to be meaningful.
    """
    # Normalise separators
    cleaned = raw.replace(" - ", "_").replace("-", "_").replace(" ", "_")
    parts   = cleaned.split("_")
    result  = []
    for p in parts:
        p = p.strip(".,!?;:'\"()[]%#@!").lower()
        if (len(p) > 3
                and p not in STOPWORDS
                and p not in GENERIC_TECH_WORDS
                and p not in GENERIC_PATH_PARTS
                and not p.startswith("http")
                and "%" not in p          # skip URL-encoded parts
                and "." not in p          # skip domain-like tokens (linkedin.com etc.)
                and not _is_hex_noise(p)  # skip UUID fragments / git hashes
                ):
            result.append(p)
    return result


def _extract_from_path(fpath: str) -> set[str]:
    """
    Extract project fingerprint tokens from a single file path entry.
    Handles real paths, file:// URLs (mine the filename), and skips
    http/https/vscode-file:// entirely (no useful project info in path).
    """
    tokens = set()
    path   = fpath.strip()

    # Case 1: file:// URL — extract the filename at the end
    if "file://" in path.lower():
        # file:///C:/Users/kushagra/Downloads/loom_council_debate.html
        fname = path.split("/")[-1].split("?")[0]
        stem  = fname.rsplit(".", 1)[0]
        tokens.update(_tokenise_name(stem))
        return tokens

    # Case 2: other URL schemes — skip entirely (no project info)
    if any(path.lower().startswith(p) for p in _URL_PREFIXES):
        return tokens

    # Case 3: real file path — mine folder names AND filename stem
    normalised = path.replace("\\", "/")
    parts      = normalised.split("/")

    # Folder parts (all but last)
    for part in parts[:-1]:
        for tok in _tokenise_name(part):
            tokens.add(tok)

    # Filename stem — e.g. "loom_pitch.docx" → "loom", "pitch"
    # Also handles VS Code window title stored as filename:
    # ".gitignore - loom - Visual Studio Code"
    fname = parts[-1]
    raw_stem = fname.rsplit(".", 1)[0]
    # rsplit on a dotfile like ".gitignore" returns "" — fall back to full name
    stem = raw_stem if raw_stem else fname
    for tok in _tokenise_name(stem):
        tokens.add(tok)

    return tokens


def _project_fingerprint(node: dict) -> set[str]:
    """
    Project-level tokens: specific enough to name a project,
    not generic tools, languages, or apps.

    Sources (ordered by reliability):
    1. Filename stems + folder names from files_touched
       - Real paths: both folder names and filename stems
       - file:// URLs: filename stem (e.g. loom_council_debate.html → "loom")
       - http(s)/vscode-file URLs: skipped entirely
    2. Long specific keywords not in generic tech set
    3. Proper nouns in summary/intent that are ALSO in keywords (double-confirmed)
    """
    tokens = set()

    # ── Source 1: file paths ──────────────────────────────────────────────
    for fpath in _parse_list(node.get("files_touched")):
        tokens.update(_extract_from_path(fpath))

    # ── Source 2: keyword tokens (specific, long) ─────────────────────────
    for kw in _parse_list(node.get("keywords")):
        for token in kw.lower().split():
            token = token.strip(".,!?;:'\"()[]%#")
            if (len(token) > 4
                    and token not in STOPWORDS
                    and token not in GENERIC_TECH_WORDS
                    and not _is_hex_noise(token)
                    and "." not in token):
                tokens.add(token)

    # ── Source 3: proper nouns confirmed by keywords ───────────────────────
    # "Loom" appears capitalised in summary AND "loom" is in keyword list
    # → high-confidence project name
    kw_tokens = _node_keyword_set(node)
    for field in ["summary", "intent"]:
        text  = node.get(field) or ""
        words = text.split()
        for idx, word in enumerate(words):
            clean = word.strip(".,!?;:'\"()[]")
            lower = clean.lower()
            if (idx > 0                        # not sentence-start
                    and len(clean) > 3
                    and clean[0].isupper()
                    and lower not in STOPWORDS
                    and lower not in GENERIC_TECH_WORDS
                    and lower in kw_tokens):   # confirmed by LLM keyword
                tokens.add(lower)

    return tokens


def _fingerprint_sim(a: set, b: set) -> float:
    """
    Non-linear fingerprint similarity.
    Any shared specific token = strong signal. More shared = stronger.
    """
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    # First shared token: 0.50 boost. Each additional: +0.20, capped at 1.0
    return min(1.0, 0.50 + (len(shared) - 1) * 0.20)


# ── Combined similarity ─────────────────────────────────────────────────────

def _combined_sim(
    ni: dict, nj: dict,
    ni_v: np.ndarray | None, nj_v: np.ndarray | None,
    ni_kw: set, nj_kw: set,
    ni_sw: set, nj_sw: set,
    ni_fp: set, nj_fp: set,
) -> float:
    """
    Five-signal similarity between two memory nodes.

    Weights designed so that:
    - Two sessions sharing a project name + same day → merge (even if different tools)
    - Two sessions with high semantic similarity → merge
    - Unrelated sessions (music vs coding, different projects) → stay separate
    """
    # A — vector cosine (semantic meaning)
    vec_sim = 0.0
    if ni_v is not None and nj_v is not None:
        vec_sim = _cosine(ni_v, nj_v)

    # B — keyword Jaccard
    kw_sim = _jaccard(ni_kw, nj_kw)

    # C — summary word overlap
    shared_words = ni_sw & nj_sw
    sw_sim = min(1.0, len(shared_words) / SUMMARY_SHARED_MIN)

    # D — temporal proximity (tool-switch context)
    temporal = _temporal_sim(ni, nj)

    # E — project fingerprint (project-name bridge)
    fp_sim = _fingerprint_sim(ni_fp, nj_fp)

    combined = (
        vec_sim  * 0.38 +
        kw_sim   * 0.15 +
        sw_sim   * 0.05 +
        temporal * 0.17 +
        fp_sim   * 0.25
    )
    return combined


# ── Two-pass clustering ─────────────────────────────────────────────────────
#
# PASS 1 — Fingerprint union-find:
#   Nodes sharing any project fingerprint token (e.g. both have "loom")
#   are definitively the same project and merged unconditionally.
#   This is the "project identity" pass.
#
# PASS 2 — Similarity absorption:
#   Nodes/clusters not connected by fingerprints are checked against
#   existing clusters using average-linkage similarity.
#   A node joins the closest cluster IF avg similarity ≥ threshold.
#   This pulls in related sessions that mention the project implicitly.

def _fingerprint_components(n: int, fp_list: list[set]) -> list[list[int]]:
    """
    Union-find: merge any two nodes that share at least one fingerprint token.
    Returns initial clusters — some may be large (Loom coding + pitch + docs),
    most isolated nodes start as singletons.
    """
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        parent[find(a)] = find(b)

    for i in range(n):
        if not fp_list[i]:
            continue
        for j in range(i + 1, n):
            if fp_list[j] and (fp_list[i] & fp_list[j]):
                union(i, j)

    # Group by root
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _absorb_with_similarity(
    initial_clusters: list[list[int]],
    n: int,
    sim_matrix: dict[tuple, float],
    threshold: float,
) -> list[list[int]]:
    """
    Second pass: average-linkage absorption.
    Singleton clusters (nodes not merged by fingerprint) try to join
    an existing multi-node cluster if their average similarity ≥ threshold.
    Multi-node fingerprint clusters are kept intact — they are never split.
    """
    # Separate fingerprint-merged groups from singletons
    merged   = [c for c in initial_clusters if len(c) > 1]
    isolated = [c[0] for c in initial_clusters if len(c) == 1]

    # Sort isolated nodes by their index (= timestamp order) for determinism
    isolated.sort()

    # Each fingerprint group starts as a locked cluster
    clusters: list[list[int]] = [list(c) for c in merged]

    def avg_sim_to_cluster(node_idx: int, cluster: list[int]) -> float:
        sims = [
            sim_matrix.get((min(node_idx, j), max(node_idx, j)), 0.0)
            for j in cluster
        ]
        return sum(sims) / len(sims) if sims else 0.0

    for i in isolated:
        best_cluster = -1
        best_avg     = -1.0

        for ci, cluster in enumerate(clusters):
            avg = avg_sim_to_cluster(i, cluster)
            if avg > best_avg:
                best_avg     = avg
                best_cluster = ci

        if best_cluster >= 0 and best_avg >= threshold:
            clusters[best_cluster].append(i)
        else:
            # Start a new singleton cluster
            clusters.append([i])

    return clusters


# ── Project name inference ─────────────────────────────────────────────────

def _infer_name(nodes: list[dict]) -> str:
    """
    Multi-strategy name inference — ordered from most to least reliable.
    Project fingerprint tokens are weighted first since they're project-specific.
    """
    # Strategy 0 — fingerprint tokens that recur (most project-specific)
    # Requires the token to appear in at least half the nodes AND ≥ 2 nodes
    # (prevents a song title from one node naming a 2-session music cluster)
    fp_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for token in _project_fingerprint(node):
            fp_counts[token] += 1

    if fp_counts:
        top_fp    = max(fp_counts, key=fp_counts.get)
        top_count = fp_counts[top_fp]
        min_count = max(2, len(nodes) // 2)   # need ≥ 2 nodes to use fp as name
        if top_count >= min_count:
            return top_fp.replace("-", " ").replace("_", " ").title()

    # Strategy 1 — proper noun appearing in summaries (capitalised mid-sentence)
    proper_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        summary = node.get("summary") or ""
        words   = summary.split()
        for idx, word in enumerate(words):
            clean = word.strip(".,!?;:'\"()[]")
            if (len(clean) > 2
                    and clean[0].isupper()
                    and clean.lower() not in STOPWORDS
                    and clean.lower() not in GENERIC_TECH_WORDS
                    and idx > 0):
                proper_counts[clean.lower()] += 1

    if proper_counts:
        all_kw_tokens = set()
        for node in nodes:
            all_kw_tokens.update(_node_keyword_set(node))
        boosted = {
            w: cnt * (3 if w in all_kw_tokens else 1)
            for w, cnt in proper_counts.items()
        }
        top_noun = max(boosted, key=boosted.get)
        # Use as name if: appears in majority of nodes, OR keyword-confirmed
        is_kw_confirmed = top_noun in all_kw_tokens
        meets_majority  = proper_counts[top_noun] >= max(2, len(nodes) // 2)
        if meets_majority or is_kw_confirmed:
            return top_noun.title()

    # Strategy 2 — most common keyword token (prefer multi-node)
    kw_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for token in _node_keyword_set(node):
            if token not in GENERIC_TECH_WORDS:
                kw_counts[token] += 1

    if kw_counts:
        top_kw    = max(kw_counts, key=kw_counts.get)
        top_count = kw_counts[top_kw]
        if top_count >= max(1, len(nodes) // 2):
            return top_kw.replace("-", " ").replace("_", " ").title()
        longest_kw = max(kw_counts.keys(), key=len)
        if len(longest_kw) > 5:
            return longest_kw.replace("-", " ").replace("_", " ").title()

    # Strategy 3 — summary word frequency
    word_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for word in _summary_words(node):
            if word not in GENERIC_TECH_WORDS:
                word_counts[word] += 1

    if word_counts:
        top = max(word_counts, key=word_counts.get)
        if word_counts[top] >= max(1, len(nodes) // 3):
            return top.replace("-", " ").replace("_", " ").title()

    # Strategy 4 — most common non-generic folder
    folder_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for fpath in _parse_list(node.get("files_touched")):
            parts = fpath.replace("\\", "/").split("/")
            for part in parts[:-1]:
                clean = part.lower().strip()
                if (clean
                        and clean not in GENERIC_PATH_PARTS
                        and clean not in GENERIC_TECH_WORDS
                        and len(clean) > 2):
                    folder_counts[part] += 1
    if folder_counts:
        return max(folder_counts, key=folder_counts.get).replace("-", " ").replace("_", " ").title()

    # Strategy 5 — distinctive filename stem
    file_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for fpath in _parse_list(node.get("files_touched")):
            fname = fpath.replace("\\", "/").split("/")[-1]
            stem  = fname.rsplit(".", 1)[0]
            clean = stem.replace("_", " ").replace("-", " ").strip()
            if len(clean) > 4 and clean.lower() not in STOPWORDS:
                file_counts[clean] += 1
    if file_counts:
        return max(file_counts, key=file_counts.get).title()

    # Strategy 6 — first meaningful words of most recent summary
    recent  = sorted(nodes, key=lambda n: n.get("timestamp", ""), reverse=True)
    summary = (recent[0].get("summary") or "") if recent else ""
    words   = [
        w for w in summary.split()[:6]
        if w.lower() not in STOPWORDS and w.lower() not in GENERIC_TECH_WORDS
    ]
    return " ".join(words) if words else "Unnamed Project"


def _collect_keywords(nodes: list[dict]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        for kw in _parse_list(node.get("keywords")):
            counts[kw.lower()] += 1
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [k for k, _ in top[:10]]


# ── Main build function ────────────────────────────────────────────────────

async def build_projects():
    """
    Cluster memory nodes into projects using a 5-signal similarity:
    vector cosine + keywords + summary words + temporal proximity + project fingerprint.
    """
    print("[Projects] Building project clusters (v3)...")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM memory_nodes ORDER BY timestamp ASC'
        ) as cur:
            all_nodes = [dict(r) for r in await cur.fetchall()]

    if not all_nodes:
        print("[Projects] No memory nodes yet")
        return 0

    n       = len(all_nodes)
    vectors = _load_vectors()

    # Precompute per-node derived signals (avoid recomputing in O(n²) loop)
    node_kw = [_node_keyword_set(nd) for nd in all_nodes]
    node_sw = [_summary_words(nd)    for nd in all_nodes]
    node_fp = [_project_fingerprint(nd) for nd in all_nodes]
    node_v  = [vectors.get(nd["id"]) for nd in all_nodes]

    # Log nodes with fingerprints (compact)
    fp_node_count = sum(1 for fp in node_fp if fp)
    print(f"[Projects] {fp_node_count}/{n} nodes have project fingerprints")

    # Pairwise similarity matrix
    sim_matrix: dict[tuple, float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = _combined_sim(
                all_nodes[i], all_nodes[j],
                node_v[i],    node_v[j],
                node_kw[i],   node_kw[j],
                node_sw[i],   node_sw[j],
                node_fp[i],   node_fp[j],
            )
            sim_matrix[(i, j)] = sim

    # Pass 1: fingerprint-connected components (project identity)
    fp_clusters = _fingerprint_components(n, node_fp)
    print(f"[Projects] Fingerprint components: {[[all_nodes[i]['id'] for i in c] for c in fp_clusters if len(c) > 1]}")

    # Pass 2: absorb isolated nodes into fingerprint groups via similarity
    components = _absorb_with_similarity(fp_clusters, n, sim_matrix, VECTOR_SIM_THRESHOLD)

    # Save projects
    saved = 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM projects')

        for component in components:
            nodes_in = [all_nodes[idx] for idx in component]
            if len(nodes_in) < MIN_SESSIONS:
                continue

            name          = _infer_name(nodes_in)
            node_ids      = [nd["id"] for nd in nodes_in]
            timestamps    = [nd.get("timestamp", "") for nd in nodes_in]
            first_seen    = min(timestamps)
            last_active   = max(timestamps)
            session_count = len(nodes_in)
            keywords      = _collect_keywords(nodes_in)

            try:
                last_dt  = datetime.fromisoformat(last_active)
                is_stale = 1 if (datetime.now() - last_dt).days >= STALE_DAYS else 0
            except Exception:
                is_stale = 0

            print(f"  [{session_count} session(s)] {name} — nodes {node_ids}")

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


# ── Queries ────────────────────────────────────────────────────────────────

async def get_all_projects() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM projects ORDER BY last_active DESC'
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        p = dict(r)
        for field in ["node_ids", "keywords"]:
            try:
                p[field] = json.loads(p.get(field) or "[]")
            except Exception:
                p[field] = []
        result.append(p)
    return result


async def get_stale_projects() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM projects WHERE is_stale = 1 ORDER BY last_active ASC'
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        p = dict(r)
        for field in ["node_ids", "keywords"]:
            try:
                p[field] = json.loads(p.get(field) or "[]")
            except Exception:
                p[field] = []
        result.append(p)
    return result
