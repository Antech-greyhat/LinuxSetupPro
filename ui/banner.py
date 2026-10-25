"""Startup and end-of-run banners with the developer info block."""

import getpass
import shutil
import socket
import subprocess
import urllib.request
from datetime import datetime

from rich.align import Align
from rich.box import DOUBLE, ROUNDED
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from core import detector, updateCheck
from ui import menu, theme

# Generated with figlet (small font); embedded to avoid a runtime dependency.
_LOGO = r"""
 _    _               ___      _             ___
| |  (_)_ _ _  ___ __/ __| ___| |_ _  _ _ __| _ \_ _ ___
| |__| | ' \ || \ \ /\__ \/ -_)  _| || | '_ \  _/ '_/ _ \
|____|_|_||_\_,_/_\_\|___/\___|\__|\_,_| .__/_| |_| \___/
                                       |_|
""".strip("\n")

_LOGO_DONE = r"""
 ___ ___ _____ _   _ ___    ___ ___  __  __ ___ _    ___ _____ ___
/ __| __|_   _| | | | _ \  / __/ _ \|  \/  | _ \ |  | __|_   _| __|
\__ \ _|  | | | |_| |  _/ | (_| (_) | |\/| |  _/ |__| _|  | | | _|
|___/___| |_|  \___/|_|    \___\___/|_|  |_|_| |____|___| |_| |___|
""".strip("\n")

_DEV = [
    ("DEVELOPER", "Antech"),
    ("GITHUB", "https://github.com/Antech-greyhat"),
    ("TELEGRAM", "https://t.me/AntechDevSecOps"),
    ("X", "https://x.com/Antech1629"),
    ("LINKEDIN", "https://www.linkedin.com/in/antony-mwendwa-07679336b"),
]

_LABEL_WIDTH = 10


def _kv_block(rows, value_style="white"):
    """Render aligned '[+] LABEL > value' rows as a single Text."""
    body = Text()
    for i, (label, value) in enumerate(rows):
        body.append("[+] ", style="green")
        body.append(f"{label:<{_LABEL_WIDTH}}", style=theme.HEADING)
        body.append("> ", style=theme.DIM)
        body.append(value, style=value_style)
        if i < len(rows) - 1:
            body.append("\n")
    return body


def _logo_panel(logo, subtitle, title, border_style):
    header = Text(logo, style=border_style)
    group = Group(Align.center(header), Align.center(Text(subtitle, style=theme.DIM)))
    return Panel(group, box=DOUBLE, border_style=border_style, title=title,
                 title_align="center", padding=(1, 3), expand=False)


def _info_panel(rows, title, border_style, value_style="cyan"):
    return Panel(_kv_block(rows, value_style), box=ROUNDED, border_style=border_style,
                 title=title, title_align="left", padding=(0, 2), expand=False)


def _local_ip():
    """Primary outbound-interface IP without sending any packets."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return "unavailable"
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "unavailable"
    finally:
        s.close()


def _username():
    # getpass raises when neither $USER-style variables nor a passwd entry exist (e.g. containers
    # running under an arbitrary UID); the session panel must not crash startup over it.
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def _public_ip():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            return resp.read().decode("utf-8").strip() or "unavailable"
    except Exception:
        return "unavailable"


def _session_rows(platform_info, public_ip=False):
    now = datetime.now()
    ip = _public_ip() if public_ip else _local_ip()
    if platform_info is not None:
        platform_desc = f"{platform_info.pretty_name} (backend: {platform_info.backend_key})"
    else:
        platform_desc = "unknown"
    return [
        ("USER", _username()),
        ("HOST", socket.gethostname()),
        ("PLATFORM", platform_desc),
        ("IP", ip),
        ("DATE", now.strftime("%d %B %Y")),
        ("TIME", now.strftime("%H:%M")),
    ]


def show_startup(version, console=None, platform_info=None, public_ip=False):
    console = console or Console()
    console.print()
    console.print(_logo_panel(_LOGO, f"v{version}  cross-platform setup and hardening",
                              "[ BUILT BY ANTECH ]", theme.BRAND))
    console.print(_info_panel(_DEV, "[ DEVELOPER ]", theme.DEV))
    console.print(_info_panel(_session_rows(platform_info, public_ip), "[ SESSION ]",
                              theme.BRAND, value_style="green"))
    notice = updateCheck.startup_notice(version)
    if notice:
        menu.notice(console, notice, title="[ UPDATE ]", border_style=theme.ACCENT)
    console.print(Text("If this tool saves you time, a star on GitHub is appreciated.", style=theme.DIM))
    console.print()


def show_end(version, platform_info, console=None, ask_open=True):
    console = console or Console()
    console.print()
    console.print(_logo_panel(_LOGO_DONE, f"LinuxSetupPro v{version}  setup finished",
                              "[ COMPLETE ]", theme.DEV))
    console.print(_info_panel(
        [("AUTHOR", "Antech"),
         ("GITHUB", "https://github.com/Antech-greyhat"),
         ("TELEGRAM", "https://t.me/AntechDevSecOps")],
        "[ SUPPORT DEVELOPMENT ]", theme.DEV))
    console.print()

    if not ask_open:
        return
    if menu.confirm(console, "Open GitHub in browser now?", default=False):
        _open_url("https://github.com/Antech-greyhat", platform_info)


def _open_url(url, platform_info):
    if platform_info and platform_info.platform == detector.TERMUX and shutil.which("termux-open-url"):
        opener = ["termux-open-url", url]
    elif shutil.which("xdg-open"):
        opener = ["xdg-open", url]
    else:
        return
    try:
        subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
