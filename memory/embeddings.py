# memory/embeddings.py
#
# WHAT THIS FILE DOES:
# Converts text into vector embeddings using nomic-embed-text via Ollama.
# A vector embedding is a list of numbers that captures the semantic
# meaning of text — similar topics produce similar vectors.
#
# WHY EMBEDDINGS:
# They let us find related memory nodes without exact keyword matching.
# "JWT authentication bug" and "token expiry issue" have different words
# but similar embeddings — so the graph knows they're related.
#
# HOW IT WORKS:
# We send text to nomic-embed-text (running locally via Ollama).
# It returns a 768-dimensional vector — a list of 768 numbers.
# We store these vectors in LanceDB for fast similarity search.
#
# WHAT WE EMBED:
# Each memory node gets embedded as a combination of its most
# semantically rich fields: summary + intent + keywords.
# This gives the embedding maximum signal about what was worked on.


import ollama
from typing import Optional


# The embedding model — must be pulled via: ollama pull nomic-embed-text
EMBED_MODEL = "nomic-embed-text"

# Dimension of nomic-embed-text output vectors
EMBED_DIMENSION = 768


def build_embed_text(node: dict) -> str:
    """
    Builds the text string we embed for a memory node.
    Combines the most semantically rich fields into one string.

    We weight the fields by importance:
    - summary   → most important, describes what happened
    - intent    → second, describes the goal
    - keywords  → specific technical terms, high signal
    - blockers  → included if present, signals struggle areas
    - files     → file names give context about the codebase area

    Returns a clean combined string ready for embedding.
    """
    parts = []

    summary = node.get("summary")
    if summary:
        parts.append(summary)

    intent = node.get("intent")
    if intent:
        parts.append(intent)

    # Keywords are already the most distilled signal
    keywords = node.get("keywords", [])
    if isinstance(keywords, list) and keywords:
        parts.append("Keywords: " + ", ".join(keywords))
    elif isinstance(keywords, str) and keywords:
        parts.append("Keywords: " + keywords)

    blockers = node.get("blockers")
    if blockers and blockers not in (None, "null", "NULL"):
        parts.append("Blocker: " + blockers)

    files = node.get("files_touched", [])
    if isinstance(files, list) and files:
        parts.append("Files: " + ", ".join(files[:5]))

    return ". ".join(parts) if parts else "general work session"


def embed_text(text: str) -> Optional[list[float]]:
    """
    Converts a text string into a vector embedding.
    Uses nomic-embed-text running locally via Ollama.

    Returns a list of 768 floats, or None if embedding fails.

    Example:
        vector = embed_text("debugging JWT authentication in auth.py")
        # Returns [0.023, -0.412, 0.891, ...] — 768 numbers
    """
    if not text or not text.strip():
        return None

    try:
        response = ollama.embeddings(
            model=EMBED_MODEL,
            prompt=text.strip()
        )
        return response["embedding"]

    except Exception as e:
        print(f"[Embeddings] Failed to embed text: {e}")
        print(f"[Embeddings] Is nomic-embed-text pulled? Run: ollama pull nomic-embed-text")
        return None


def embed_node(node: dict) -> Optional[list[float]]:
    """
    Generates an embedding vector for a memory node.
    Builds the embed text from node fields then calls embed_text().

    Returns the vector or None if embedding fails.
    """
    text = build_embed_text(node)
    return embed_text(text)


def embed_query(query: str) -> Optional[list[float]]:
    """
    Generates an embedding for a search query.
    Used when querying the vector store for similar nodes.

    Example:
        vector = embed_query("what was I working on yesterday?")
        # Then search LanceDB for nodes with similar vectors
    """
    return embed_text(query)