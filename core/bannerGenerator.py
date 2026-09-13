"""Generates a persistent terminal banner rendered with figlet and lolcat."""

import shlex
import shutil
import subprocess

from rich.console import Console

from core import shellConfig
from ui import theme

FONTS = ["Slant", "Block", "Cyberlarge", "Doom", "Small"]
COLOR_SCHEMES = ["Matrix green", "Cyberpunk", "Blood red", "Monochrome", "Rainbow"]

_FONT_FILES = {
    "Slant": "slant",
    "Block": "block",
    "Cyberlarge": "cyberlarge",
    "Doom": "doom",
    "Small": "small",
}

# ANSI SGR codes for solid schemes; Rainbow is handled by piping through lolcat.
_SCHEME_ANSI = {
    "Matrix green": "1;32",
    "Cyberpunk": "1;35",
    "Blood red": "1;31",
    "Monochrome": "0",
}

_DEFAULT_FONT = "Slant"
_DEFAULT_SCHEME = "Matrix green"


class BannerGenerator:
    def __init__(self, backend, console=None):
        self.backend = backend
        self.console = console or Console()

    def ensure_tools(self):
        """Install figlet and lolcat via the backend if missing."""
        for tool, pkg in (("figlet", "figlet"), ("lolcat", "lolcat")):
            if shutil.which(tool) is None:
                self.console.print(f"Installing {tool}...", style=theme.DIM)
                self.backend.install(pkg)

    def build_command(self, text, font, scheme):
        """Return a single shell command string that renders the banner."""
        font_file = _FONT_FILES.get(font, "standard")
        figlet = f"figlet -f {shlex.quote(font_file)} {shlex.quote(text)}"
        if scheme == "Rainbow":
            return f"{figlet} | lolcat -f"
        code = _SCHEME_ANSI.get(scheme, "0")
        return f"printf '\\033[{code}m'; {figlet}; printf '\\033[0m'"

    def render_preview(self, text, font, scheme):
        command = self._command_with_fallback(text, font, scheme)
        code, out, err = self._shell(command)
        if code != 0:
            self.console.print(f"Preview failed: {err.strip() or 'unknown error'}", style=theme.ERROR)
            return None
        self.console.print(out, end="")
        return command

    def persist(self, command, shell=None):
        path = shellConfig.insert_banner_block([command], shell=shell, console=self.console)
        return path

    def _command_with_fallback(self, text, font, scheme):
        command = self.build_command(text, font, scheme)
        code, _, _ = self._shell(command)
        if code != 0:
            # Requested figlet font is missing; fall back to the always-present standard font.
            self.console.print(f"Font '{font}' unavailable, using standard.", style=theme.WARN)
            return self.build_command(text, "Standard", scheme)
        return command

    def _shell(self, command):
        try:
            proc = subprocess.run(["bash", "-c", command], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=30, check=False)
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)


def default_settings():
    return _DEFAULT_FONT, _DEFAULT_SCHEME
