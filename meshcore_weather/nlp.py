"""Simple command parser. No LLM needed.

Supported formats:
    wx Austin TX
    wx KAUS
    forecast Miami FL
    warn FL
    metar KJFK
    taf KJFK
    help
"""

import re


# Command must be first word, followed by location
COMMAND_RE = re.compile(
    r"^(wx|warn|warnings?|wanr|forecast|metar|taf|help|more|outlook|rain|storm|storms)\b\s*(.*)",
    re.IGNORECASE,
)

# Normalize typos/aliases to canonical command names
_CMD_ALIASES = {"warnings": "warn", "warning": "warn", "wanr": "warn", "storms": "storm"}


async def parse_intent(text: str) -> dict:
    text = text.strip()
    if not text:
        return {"command": "help", "location": ""}

    m = COMMAND_RE.match(text)
    if m:
        cmd = m.group(1).lower()
        cmd = _CMD_ALIASES.get(cmd, cmd)
        loc = m.group(2).strip()
        # Strip filler words
        for prefix in ["for ", "in ", "near ", "around "]:
            if loc.lower().startswith(prefix):
                loc = loc[len(prefix):]
        return {"command": cmd, "location": loc}

    # No recognized command - assume "wx" with the whole text as location
    return {"command": "wx", "location": text}
