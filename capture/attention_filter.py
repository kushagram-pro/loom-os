# attention_filter.py
# Sits between the raw event queue and the database.
# Its job: decide what's worth remembering before writing to disk.
# This is the computational attention layer of Loom.
#
# Rules for v0 — simple heuristics, no AI needed yet:
# - Duration too short → drop
# - System noise process → drop
# - Duplicate of last event → drop
# - High-value app → flag as important

# Apps considered high cognitive value
HIGH_VALUE_APPS = {
    'code', 'cursor', 'vim', 'nvim', 'sublime_text',
    'chrome', 'firefox', 'edge', 'brave',
    'notion', 'obsidian', 'roamresearch',
    'slack', 'discord', 'teams',
    'terminal', 'windowsterminal', 'powershell', 'cmd',
    'figma', 'excel', 'word', 'powerpoint',
    'jupyter', 'pycharm', 'webstorm', 'intellij'
}

# Process names that are pure noise
NOISE_PROCESSES = {
    'explorer', 'searchhost', 'shellexperiencehost',
    'startmenuexperiencehost', 'textinputhost',
    'runtimebroker', 'svchost', 'ctfmon',
    'applicationframehost', 'systemsettings',
    'lockapp', 'logonui', 'winlogon'
}

# Minimum duration to store a window event (seconds)
MIN_WINDOW_DURATION = 4


class AttentionFilter:
    """
    Stateful filter that decides which events are worth storing.
    Keeps track of the last event to detect duplicates.
    """

    def __init__(self):
        self.last_event = None

    def should_store(self, event: dict) -> tuple[bool, str]:
        """
        Returns (should_store: bool, importance: str)
        importance is 'high', 'normal', or 'low'
        """
        source = event.get("source", "")
        app = event.get("app", "").lower().replace('.exe', '')
        title = event.get("title", "")
        duration = event.get("duration", 0)

        # Always store rhythm events — they're already filtered
        if source == "rhythm":
            return True, "normal"

        # Always store clipboard — user explicitly copied something
        if source == "clipboard":
            return True, "high"

        # Drop noise processes
        if app in NOISE_PROCESSES:
            return False, ""

        # Drop very short window sessions
        if source == "system" and duration < MIN_WINDOW_DURATION:
            return False, ""

        # Drop exact duplicates
        if self.last_event:
            if (self.last_event.get("app") == event.get("app") and
                    self.last_event.get("title") == event.get("title")):
                return False, ""

        # Determine importance
        importance = "high" if app in HIGH_VALUE_APPS else "normal"

        self.last_event = event
        return True, importance

    def enrich(self, event: dict, importance: str) -> dict:
        """Adds importance metadata to the event before storing."""
        event["importance"] = importance
        return event