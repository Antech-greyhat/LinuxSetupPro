"""Centralized colors and status markers. No emojis anywhere."""

from core.packageManager import (
    ALREADY_INSTALLED,
    INSTALLED,
    INSTALLED_UNVERIFIED,
    UPGRADED,
    SKIPPED,
    FAILED,
)

# rich style strings keyed by status.
STATUS_COLORS = {
    ALREADY_INSTALLED: "green",
    INSTALLED: "green",
    INSTALLED_UNVERIFIED: "dark_orange",
    UPGRADED: "cyan",
    SKIPPED: "yellow",
    FAILED: "red",
}

# Plain-text markers used where color alone is not enough.
MARKER_OK = "[OK]"
MARKER_FAIL = "[FAIL]"
MARKER_SKIP = "[SKIP]"
MARKER_LOCKED = "[LOCKED]"

# Named roles so menus and prompts share one palette instead of hardcoding colors.
BRAND = "bold cyan"     # startup logo and session panel border
ACCENT = "cyan"         # menu panel borders and option keys; matches the banner's cyan
DEV = "magenta"         # developer, support, and completion panels in banner.py
LABEL = "bold white"    # option labels and field names
HINT = "dim"            # dimmed parenthetical hint under an option
PROMPT = ACCENT         # input caret and typed text; matches ACCENT
DIM = "dim"
HEADING = "bold white"
WARN = "yellow"
ERROR = "bold red"


def status_style(status):
    return STATUS_COLORS.get(status, "white")


def status_label(status):
    return status
