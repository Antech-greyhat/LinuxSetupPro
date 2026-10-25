"""Detects the shell rc file and manages marked, idempotent config blocks."""

import os
import shutil

from ui import theme

BEGIN_MARKER = "# BEGIN linux-setup-pro banner"
END_MARKER = "# END linux-setup-pro banner"

EDITOR_BEGIN = "# BEGIN linux-setup-pro editor"
EDITOR_END = "# END linux-setup-pro editor"

STARSHIP_BEGIN = "# BEGIN linux-setup-pro starship"
STARSHIP_END = "# END linux-setup-pro starship"

# Per-shell starship init. Each picks up a ~/.local/bin install (the no-root fallback) even when
# that dir is not on PATH yet, and is a no-op once starship is uninstalled.
_STARSHIP_INIT = {
    "bash": [
        '[ -x "$HOME/.local/bin/starship" ] && ! command -v starship >/dev/null 2>&1 && PATH="$HOME/.local/bin:$PATH"',
        'command -v starship >/dev/null 2>&1 && eval "$(starship init bash)"',
    ],
    "zsh": [
        '[ -x "$HOME/.local/bin/starship" ] && ! command -v starship >/dev/null 2>&1 && PATH="$HOME/.local/bin:$PATH"',
        'command -v starship >/dev/null 2>&1 && eval "$(starship init zsh)"',
    ],
    "fish": [
        "if test -x $HOME/.local/bin/starship; and not type -q starship",
        "    set -gx PATH $HOME/.local/bin $PATH",
        "end",
        "type -q starship; and starship init fish | source",
    ],
}

# The instant-prompt block always sources a p10k-instant-prompt cache file.
_P10K_SOURCE = "p10k-instant-prompt"

# One rolling backup per dotfile, written immediately before we modify it.
_BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".linuxsetuppro", "backups")


def detect_shell():
    """Return the shell base name from $SHELL, defaulting to bash."""
    shell = os.environ.get("SHELL", "")
    base = os.path.basename(shell)
    return base if base else "bash"


def rc_path_for_shell(shell=None):
    shell = shell or detect_shell()
    home = os.path.expanduser("~")
    if shell == "zsh":
        return os.path.join(home, ".zshrc")
    return os.path.join(home, ".bashrc")


def _read_lines(path):
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def _tilde(path):
    """Collapse the home directory to ~ for a friendlier printed path."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def backup_dotfile(path, console=None):
    """Copy a dotfile to the backups dir before it is modified. Returns the backup path or None.

    One rolling backup per file: each call overwrites the prior backup for that file, so it always
    holds the version from just before the most recent change. A file that does not exist yet has
    nothing to preserve and is skipped silently.
    """
    if not os.path.isfile(path):
        return None
    dest = os.path.join(_BACKUP_DIR, os.path.basename(path))
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        shutil.copy2(path, dest)
    except OSError as exc:
        if console is not None:
            console.print(f"Could not back up {_tilde(path)}: {exc}", style=theme.WARN)
        return None
    if console is not None:
        console.print(f"Backed up {_tilde(path)} to {_tilde(dest)}", style=theme.DIM)
    return dest


def _strip_block(lines, begin, end):
    """Remove existing begin/end blocks, keeping everything else.

    A begin marker with no matching end marker (e.g. hand-edited away) is left untouched, so the
    rest of the user's file is never silently discarded.
    """
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() == begin:
            close = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == end), None)
            if close is not None:
                i = close + 1
                continue
        out.append(lines[i])
        i += 1
    return out


def _p10k_start_index(lines):
    """Return the index where a p10k instant-prompt block (with its comment header) begins, or None.

    p10k requires console output to happen above this block; output below it triggers the
    'console output during zsh initialization' warning on every shell start.
    """
    first = next((i for i, line in enumerate(lines) if _P10K_SOURCE in line), None)
    if first is None:
        return None
    # Climb over the comment header p10k writes directly above its `if` guard.
    while first > 0 and lines[first - 1].lstrip().startswith("#") \
            and "linux-setup-pro" not in lines[first - 1]:
        first -= 1
    return first


def insert_banner_block(body_lines, shell=None, console=None):
    """Insert or replace the banner block. Returns the rc path written."""
    path = rc_path_for_shell(shell)
    backup_dotfile(path, console)
    lines = _read_lines(path)
    lines = _strip_block(lines, BEGIN_MARKER, END_MARKER)

    block = [BEGIN_MARKER] + body_lines + [END_MARKER]
    insert_at = _p10k_start_index(lines)
    if insert_at is None:
        new_lines = block + lines
    else:
        new_lines = lines[:insert_at] + block + lines[insert_at:]

    _write_lines(path, new_lines)
    return path


def remove_banner_block(shell=None, console=None):
    """Remove the banner block from the rc file. Returns (path, removed)."""
    path = rc_path_for_shell(shell)
    lines = _read_lines(path)
    if not any(line.strip() == BEGIN_MARKER for line in lines):
        return path, False
    backup_dotfile(path, console)
    _write_lines(path, _strip_block(lines, BEGIN_MARKER, END_MARKER))
    return path, True


def set_editor(editor, shell=None, console=None):
    """Persist an EDITOR export inside a marked block. Returns the rc path."""
    path = rc_path_for_shell(shell)
    backup_dotfile(path, console)
    lines = _strip_block(_read_lines(path), EDITOR_BEGIN, EDITOR_END)
    block = [EDITOR_BEGIN, f'export EDITOR="{editor}"', f'export VISUAL="{editor}"', EDITOR_END]
    _write_lines(path, lines + block)
    return path


def _fish_config_path():
    return os.path.join(os.path.expanduser("~"), ".config", "fish", "config.fish")


def starship_targets(fish_planned=False):
    """Config files that should initialize starship, as (shell, path) pairs.

    bash is always configured; zsh and fish only when the shell is installed or already has a
    config file (fish_planned covers a fish install queued in the same run).
    """
    targets = [("bash", rc_path_for_shell("bash"))]
    zshrc = rc_path_for_shell("zsh")
    if shutil.which("zsh") or os.path.isfile(zshrc):
        targets.append(("zsh", zshrc))
    fish = _fish_config_path()
    if fish_planned or shutil.which("fish") or os.path.isfile(fish):
        targets.append(("fish", fish))
    return targets


def set_starship_init(console=None):
    """Write the marked starship init block, last in each target config. Returns the paths written."""
    written = []
    for shell, path in starship_targets():
        backup_dotfile(path, console)
        lines = _strip_block(_read_lines(path), STARSHIP_BEGIN, STARSHIP_END)
        block = [STARSHIP_BEGIN] + _STARSHIP_INIT[shell] + [STARSHIP_END]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_lines(path, lines + block)
        except OSError as exc:
            if console is not None:
                console.print(f"Could not update {_tilde(path)}: {exc}", style=theme.WARN, markup=False)
            continue
        written.append(path)
    return written


def get_editor(shell=None):
    """Return the EDITOR value from our editor block, or None if it was never set."""
    inside = False
    for line in _read_lines(rc_path_for_shell(shell)):
        stripped = line.strip()
        if stripped == EDITOR_BEGIN:
            inside = True
        elif stripped == EDITOR_END:
            inside = False
        elif inside and stripped.startswith("export EDITOR="):
            return stripped[len("export EDITOR="):].strip().strip('"').strip("'") or None
    return None


def has_p10k(shell=None):
    return _p10k_start_index(_read_lines(rc_path_for_shell(shell))) is not None
