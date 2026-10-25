"""Generates a persistent terminal banner rendered with figlet and lolcat."""

import shlex
import shutil
import subprocess

from rich.console import Console
from rich.text import Text

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
            self.console.print(f"Preview failed: {err.strip() or 'unknown error'}", style=theme.ERROR,
                               markup=False)
            return None
        # figlet art is not rich markup, and lolcat/printf emit raw ANSI colors; decode them as such.
        self.console.print(Text.from_ansi(out), end="", soft_wrap=True)
        return command

    def persist(self, command, shell=None):
        # Only print in interactive shells: output from a non-interactive one (scp, rsync, sftp,
        # `ssh host cmd`) corrupts the protocol stream and breaks those tools.
        body = ["case $- in", f"  *i*) {command} ;;", "esac"]
        path = shellConfig.insert_banner_block(body, shell=shell, console=self.console)
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
            # pipefail so a figlet failure (missing font) inside `figlet | lolcat` is not masked
            # by lolcat's exit status, which would skip the standard-font fallback.
            proc = subprocess.run(["bash", "-o", "pipefail", "-c", command], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=30, check=False)
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)


def default_settings():
    return _DEFAULT_FONT, _DEFAULT_SCHEME
