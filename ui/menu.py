"""Interactive prompts: panels, confirmations, pickers, and the secondary checkbox menu."""

import sys
import textwrap

import questionary
from questionary import Choice
from rich.box import ROUNDED
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from ui import theme

_SELECT_ALL = "__select_all__"
# Sentinels distinguishing a whole-category toggle from an individual package in the uninstall menu.
_UNINSTALL_CATEGORY = "__cat__::"
_UNINSTALL_PACKAGE = "__pkg__::"

# Wrap option hints in code so a narrow terminal (Termux portrait, ~50 cols) never truncates them.
_HINT_WRAP = 44

# Wrap prose notices a little wider than option hints, still safe on a narrow terminal.
_NOTICE_WRAP = 58

# Cyan-accented widget theme so the questionary checkboxes match the panel menus.
_CHECKBOX_STYLE = questionary.Style([
    ("qmark", "fg:cyan bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:cyan"),
    ("answer", "fg:cyan bold"),
])


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def is_interactive():
    """True when prompts can actually be shown; callers skip interactive-only flows otherwise."""
    return _interactive()


def render_panel_menu(console, title, options, indent=0):
    """Render a titled panel of options; options is a list of (key, label, hint) tuples.

    Each option prints 'key) label', with any hint wrapped onto indented continuation
    lines aligned under the label (a hanging indent), so long hints never overflow.
    """
    body = Text()
    for i, (key, label, hint) in enumerate(options):
        if i:
            body.append("\n")
        body.append(f"{key}) ", style=theme.ACCENT)
        body.append(label, style=theme.LABEL)
        if hint:
            pad = " " * (len(key) + 2)
            # Clamp to the real terminal so the panel never overflows and rich never re-wraps mid-word.
            width = max(20, min(_HINT_WRAP, console.width - indent - 6))
            wrapped = textwrap.fill(hint, width=width, initial_indent=pad, subsequent_indent=pad)
            body.append("\n")
            body.append(wrapped, style=theme.HINT)
    panel = Panel(body, box=ROUNDED, border_style=theme.ACCENT, title=title,
                  title_align="left", padding=(0, 2), expand=False)
    console.print(Padding(panel, (0, 0, 0, indent)) if indent else panel)


def notice(console, message, title=None, border_style=theme.WARN):
    """Render a prose message inside a panel matching the menu styling."""
    width = max(20, min(_NOTICE_WRAP, console.width - 6))
    wrapped = "\n".join(textwrap.fill(line, width=width) for line in message.splitlines())
    panel = Panel(Text(wrapped), box=ROUNDED, border_style=border_style, title=title,
                  title_align="left", padding=(0, 2), expand=False)
    console.print(panel)


def _prompt(console, message):
    """Themed input caret. Returns the raw string, or None if the user cancels."""
    try:
        return console.input(f"[{theme.PROMPT}]{escape(message)}[/]")
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None


def _read_choice(console, prompt, valid):
    """Prompt until the user enters one of valid (case-insensitive), or None on cancel."""
    if not _interactive():
        return None
    while True:
        answer = _prompt(console, prompt)
        if answer is None:
            return None
        answer = answer.strip().lower()
        if answer in valid:
            return answer
        console.print("  Please enter one of: " + ", ".join(sorted(valid)), style=theme.HINT)


def confirm(console, message, default=False):
    """Themed yes/no prompt. Returns default when non-interactive or on cancel."""
    if not _interactive():
        return default
    suffix = "[Y/n]" if default else "[y/n]"
    while True:
        answer = _prompt(console, f"{message} {suffix} ")
        if answer is None:
            return default
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        console.print("  Please answer y or n.", style=theme.HINT)


def text(console, message, default=""):
    """Themed free-text prompt. Returns default when empty, cancelled, or non-interactive."""
    if not _interactive():
        return default
    answer = _prompt(console, f"{message} ")
    if answer is None:
        return default
    answer = answer.strip()
    return answer if answer else default


def select(console, message, options, default=None):
    """Single-select rendered as a numbered panel; options is a list of (value, label) tuples.

    A label may carry a ' - ' separated hint, shown dimmed under the option like the menus.
    """
    fallback = default if default is not None else options[0][0]
    if not _interactive():
        return fallback
    rows = []
    for i, (_, label) in enumerate(options, start=1):
        name, _, hint = label.partition(" - ")
        rows.append((str(i), name, hint))
    render_panel_menu(console, message, rows)
    choice = _read_choice(console, "  > ", {str(i) for i in range(1, len(options) + 1)})
    if choice is None:
        return fallback
    return options[int(choice) - 1][0]


def editor_multiselect(entries):
    """Multi-select editors. entries is a list of catalog dicts. Returns selected names."""
    if not _interactive():
        return []
    choices = []
    for entry in entries:
        label = entry["name"]
        if entry.get("locked"):
            label = f"{entry['name']} {theme.MARKER_LOCKED}"
        choices.append(Choice(title=f"{label} - {entry.get('description', '')}", value=entry["name"]))
    selected = questionary.checkbox("Select editors to install (space to toggle, enter to confirm):",
                                    choices=choices, style=_CHECKBOX_STYLE).ask()
    return selected or []


def secondary_menu(entries):
    """Checkbox menu over secondary tools with a select-all option. Returns selected names."""
    if not _interactive():
        return []
    choices = [Choice(title="[Select all]", value=_SELECT_ALL)]
    for entry in entries:
        name = entry["name"]
        marker = f" {theme.MARKER_LOCKED}" if entry.get("locked") else ""
        title = f"{name}{marker} - {entry.get('description', '')}"
        choices.append(Choice(title=title, value=name))

    selected = questionary.checkbox(
        "Select security tools to install (space to toggle, enter to confirm):",
        choices=choices, style=_CHECKBOX_STYLE,
    ).ask()
    selected = selected or []
    if _SELECT_ALL in selected:
        return [entry["name"] for entry in entries]
    return selected


def _uninstall_package_value(candidate):
    return _UNINSTALL_PACKAGE + candidate["category"] + "::" + candidate["name"]


def uninstall_menu(candidates):
    """Checkbox over tool-installed packages grouped by category, each with an [All] toggle.

    candidates is a list of manifest dicts (name, category, description, ...). Selecting a
    category toggle removes everything tracked under it; individual packages remove precisely.
    Returns the chosen subset of candidate dicts, deduped and in the order given.
    """
    if not _interactive():
        return []
    by_category = {}
    for candidate in candidates:
        by_category.setdefault(candidate["category"], []).append(candidate)

    choices = []
    for category, items in by_category.items():
        choices.append(Choice(title=f"[All] {category}", value=_UNINSTALL_CATEGORY + category))
        for candidate in items:
            marker = f" {theme.MARKER_LOCKED}" if candidate.get("locked") else ""
            title = f"  {candidate['name']}{marker} - {candidate.get('description', '')}"
            choices.append(Choice(title=title, value=_uninstall_package_value(candidate)))

    selected = questionary.checkbox(
        "Select packages to uninstall (space to toggle, enter to confirm):",
        choices=choices, style=_CHECKBOX_STYLE,
    ).ask()
    selected = set(selected or [])

    chosen = []
    for candidate in candidates:
        category_picked = (_UNINSTALL_CATEGORY + candidate["category"]) in selected
        package_picked = _uninstall_package_value(candidate) in selected
        if category_picked or package_picked:
            chosen.append(candidate)
    return chosen


# Main menu options: (key, label, hint). Hints wrap in the panel, never overflow.
_MAIN_OPTIONS = [
    ("1", "Run Primary Setup", "Basic / required — install this first"),
    ("2", "Run Secondary Setup", "Advanced / security tools — requires Primary"),
    ("3", "Generate Terminal Banner", "Installs figlet / lolcat if needed"),
    ("4", "View Last Report", ""),
    ("5", "Uninstall Packages", "Remove tools this installer added"),
    ("6", "Exit", ""),
]

_REPORT_OPTIONS = [
    ("4a", "Primary Report", ""),
    ("4b", "Secondary Report", ""),
    ("4c", "Both Reports", ""),
    ("4d", "Back to Main Menu", ""),
]


def main_menu(console):
    """Render the top-level menu and return the chosen number as a string, or None to exit."""
    if not _interactive():
        return None
    console.print()
    render_panel_menu(console, "LinuxSetupPro - Main Menu", _MAIN_OPTIONS)
    return _read_choice(console, "  > ", {"1", "2", "3", "4", "5", "6"})


def report_menu(console):
    """Render the View Last Report submenu (nested under the main menu), or None to go back."""
    if not _interactive():
        return None
    console.print()
    render_panel_menu(console, "View Last Report", _REPORT_OPTIONS, indent=5)
    return _read_choice(console, "     > ", {"4a", "4b", "4c", "4d"})
