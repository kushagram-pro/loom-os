# compression/prompt.py
#
# WHAT THIS FILE DOES:
# Two jobs only:
#   1. Takes raw events and builds a prompt for Phi-4 mini
#   2. Parses the JSON response back into a Python dict
#
# WHY THIS IS ITS OWN FILE:
# The prompt is the most important piece of the compression engine.
# If the prompt is vague, the memory nodes are vague.
# If the prompt is precise, the memory nodes are precise.
# Keeping it isolated means you can improve it without
# touching the engine or scheduler logic.
#
# THE OUTPUT CONTRACT:
# We tell the model exactly what JSON shape to return.
# No preamble. No markdown. Just clean JSON.
# parse_compression_response() handles edge cases where
# the model doesn't follow instructions perfectly.


import json
import re


# ── Prompt builder ─────────────────────────────────────────────────────────

def format_events_for_prompt(events: list[dict]) -> str:
    """
    Converts raw event dicts into readable plain-English lines.
    This is what the model actually reads.

    We format each source type differently because each carries
    different semantic weight:
    - system events   → where attention was, for how long
    - vscode events   → which files were touched (highest signal)
    - clipboard events → what the developer was referencing
    - screen events   → what was visible in the UI
    - rhythm events   → quality and continuity of focus
    """
    lines = []

    for e in events:
        source   = e.get("source", "")
        app      = e.get("app", "").replace(".exe", "").strip()
        title    = e.get("title", "").strip()
        detail   = e.get("detail", "").strip()
        duration = e.get("duration", 0)
        imp      = e.get("importance", "normal")

        if source == "system":
            # Window focus — tells us where attention went
            if duration >= 4:
                mins = round(duration / 60, 1)
                dur_str = f"{mins} min" if mins >= 1 else f"{round(duration)}s"
                lines.append(f"- Focused on {app}: \"{title}\" for {dur_str}")

        elif source == "vscode":
            # File edits — strongest signal of what was being built
            lines.append(f"- Edited file: {title}")
            if detail and detail != title:
                # Include path for context but trim it
                short_path = detail.replace("\\", "/")
                parts = short_path.split("/")
                # Show last 3 path components max
                readable_path = "/".join(parts[-3:]) if len(parts) > 3 else short_path
                lines.append(f"  (path: {readable_path})")

        elif source == "clipboard":
            # What was copied — strong intent signal
            clip_type = title.replace("clipboard_", "")
            if detail:
                # Trim long clipboard content but keep enough for context
                preview = detail[:150].strip()
                if len(detail) > 150:
                    preview += "..."
                lines.append(f"- Copied {clip_type}: \"{preview}\"")

        elif source == "screen":
            # UI context — what was visible in active app
            if detail and len(detail) > 10:
                preview = detail[:120].strip()
                lines.append(f"- Screen context in {app}: \"{preview}\"")
            elif title and title != app:
                lines.append(f"- UI element in {app}: {title}")

        elif source == "rhythm":
            # Focus quality — tells us about cognitive state
            if title == "focus_burst":
                mins = round(duration / 60, 1)
                lines.append(f"- Sustained focus for {mins} minutes")
            elif title == "idle_start":
                lines.append("- Stopped working (went idle)")
            elif title == "idle_end":
                idle_mins = round(duration / 60, 1)
                lines.append(f"- Returned after {idle_mins} min break")

    return "\n".join(lines) if lines else ""


def build_compression_prompt(events: list[dict]) -> str:
    """
    Builds the full prompt string to send to Phi-4 mini.

    The prompt has four parts:
    1. Role — tells the model who it is and what it's doing
    2. Activity — the formatted raw events
    3. Output format — exact JSON structure required
    4. Rules — constraints to prevent vague or hallucinated output

    Returns empty string if there are no meaningful events to compress.
    """
    events_text = format_events_for_prompt(events)

    if not events_text.strip():
        return ""

    prompt = f"""You are a cognitive memory system for a developer. Your job is to read recent computer activity and compress it into a precise memory node.

Analyze the activity below carefully. Then respond with ONLY a JSON object — no explanation, no preamble, no markdown code fences. Just the raw JSON.

RECENT ACTIVITY:
{events_text}

Respond with exactly this JSON structure:
{{
  "summary": "One specific sentence: what was the developer working on? Name the actual file, project, or problem if visible.",
  "intent": "One sentence: what were they TRYING to accomplish? Infer the goal behind the actions.",
  "blockers": "One sentence: what slowed them down or got in the way? Write null if nothing suggests a blocker.",
  "apps_used": ["list", "of", "app", "names"],
  "files_touched": ["list", "of", "actual", "filenames"],
  "focus_quality": "high or medium or low",
  "session_type": "coding or debugging or research or communication or writing or mixed",
  "keywords": ["3 to 6 specific technical keywords about what was worked on"]
}}

Rules you must follow:
- summary must name the specific thing — never write "worked on code" or "used computer"
- intent must describe the goal, not the action — "fix auth bug" not "edited files"
- blockers must name the actual obstacle if visible — "JWT refresh logic unclear" not "had difficulty"
- focus_quality is high if there was sustained focus, low if lots of switching or idle time
- keywords must be specific and technical — "JWT", "authentication", "token expiry" not "coding", "work"
- If you genuinely cannot infer something, write null for that field
- Respond with ONLY the JSON object. Nothing before it. Nothing after it."""

    return prompt


# ── Response parser ────────────────────────────────────────────────────────

def parse_compression_response(response_text: str) -> dict | None:
    """
    Parses the JSON response from Phi-4 mini into a Python dict.

    Handles common failure modes:
    - Model wraps JSON in markdown code fences (```json ... ```)
    - Model adds explanation before or after the JSON
    - Model returns slightly malformed JSON
    - Model returns empty response

    Returns the parsed dict with all required fields guaranteed,
    or None if parsing completely fails.
    """
    if not response_text or not response_text.strip():
        return None

    text = response_text.strip()

    # ── Clean up common model formatting issues ───────────────────────────

    # Strip markdown code fences if present
    # Some models add these despite being told not to
    text = re.sub(r'^```json\s*\n?', '', text)
    text = re.sub(r'^```\s*\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    text = text.strip()

    # ── Attempt 1: Direct JSON parse ──────────────────────────────────────
    try:
        parsed = json.loads(text)
        return _validate_and_fill(parsed)
    except json.JSONDecodeError:
        pass

    # ── Attempt 2: Extract JSON object from within longer text ────────────
    # Model sometimes adds explanation before or after the JSON
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            return _validate_and_fill(parsed)
        except json.JSONDecodeError:
            pass

    # ── Attempt 3: Fix common JSON issues and retry ───────────────────────
    try:
        # Replace Python None/True/False with JSON null/true/false
        fixed = text.replace(': None', ': null')
        fixed = fixed.replace(':None', ':null')
        fixed = fixed.replace(': True', ': true')
        fixed = fixed.replace(': False', ': false')
        parsed = json.loads(fixed)
        return _validate_and_fill(parsed)
    except json.JSONDecodeError:
        pass

    # All attempts failed
    return None


def _validate_and_fill(parsed: dict) -> dict:
    """
    Ensures all required fields exist in the parsed dict.
    Fills missing fields with safe defaults rather than crashing.
    Normalises list fields to always be lists.
    """
    # Required string fields — fill with None if missing
    string_fields = [
        "summary", "intent", "blockers",
        "focus_quality", "session_type"
    ]
    for field in string_fields:
        if field not in parsed:
            parsed[field] = None

    # Normalize "null" string to actual None
    for field in string_fields:
        if parsed.get(field) in ("null", "NULL", "Null", ""):
            parsed[field] = None

    # Required list fields — fill with empty list if missing or wrong type
    list_fields = ["apps_used", "files_touched", "keywords"]
    for field in list_fields:
        if not isinstance(parsed.get(field), list):
            parsed[field] = []

    # Normalize focus_quality to valid values
    valid_focus = {"high", "medium", "low"}
    if parsed.get("focus_quality") not in valid_focus:
        parsed["focus_quality"] = "medium"

    # Normalize session_type to valid values
    valid_types = {
        "coding", "debugging", "research",
        "communication", "writing", "mixed"
    }
    if parsed.get("session_type") not in valid_types:
        parsed["session_type"] = "mixed"

    return parsed