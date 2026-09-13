"""LinuxSetupPro entry point: CLI parsing and full install orchestration."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time

from rich.console import Console

from core import detector, installer, report, shellConfig, state
from core.bannerGenerator import BannerGenerator, FONTS, COLOR_SCHEMES, default_settings
from core.errorClassifier import ErrorClassifier
from core.packageManager import get_backend, OperationResult, INSTALLED, FAILED, SKIPPED
from core.report import PackageRecord, Report
from ui import banner, menu
from ui import theme

VERSION = "2.0.0"

# Exact upgrade command shown when the interpreter is too old, per platform.
_PYTHON_UPGRADE = {
    detector.TERMUX: "pkg upgrade python",
    detector.DEBIAN: "sudo apt update && sudo apt install python3.10",
    detector.FEDORA: "sudo dnf install python3.11",
    detector.ARCH: "sudo pacman -Syu python",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="linux-setup-pro",
        description="Cross-platform setup and hardening tool for Termux and Linux.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without executing anything")
    parser.add_argument("--only", choices=["primary", "secondary"],
                        help="run only the primary categories or only the secondary menu")
    parser.add_argument("--set-editor", action="store_true",
                        help="reopen the editor picker, update EDITOR, then exit")
    parser.add_argument("--report", action="store_true",
                        help="print the saved installation reports and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--no-banner", action="store_true",
                        help="suppress the startup and end-of-run banners")
    parser.add_argument("--verbose", action="store_true",
                        help="show raw subprocess output instead of progress widgets")
    parser.add_argument("--remove-banner", action="store_true",
                        help="remove the persistent shell banner block and exit")
    parser.add_argument("--public-ip", action="store_true",
                        help="show public IP in the startup session block (makes an outbound request)")
    return parser


def check_python_version(platform_info, console):
    if sys.version_info >= (3, 8):
        return
    command = _PYTHON_UPGRADE.get(platform_info.platform, "install Python 3.8 or newer")
    console.print(f"Python 3.8+ is required (found {sys.version.split()[0]}).", style=theme.ERROR)
    console.print(f"Upgrade with: {command}", style=theme.WARN)
    sys.exit(1)


def resolve_platform(console):
    platform_info = detector.detect()
    if platform_info is None:
        console.print("Unsupported or unrecognized platform. Aborting.", style=theme.ERROR)
        sys.exit(1)
    return platform_info


def run_primary(inst, catalog, plat, console, dry_run, verbose):
    records = []
    started = time.monotonic()
    for block in catalog.get("primary", []):
        category = block.get("category", "")
        entries = []
        for entry in block.get("packages", []):
            entry = dict(entry)
            entry["_category"] = category
            entries.append(entry)
        if not entries:
            continue
        console.print()
        console.print(category, style=theme.HEADING)
        records.extend(inst.install_sequence(entries))

    custom_records, notes = customization_flow(inst, catalog, plat, console, dry_run)
    records.extend(custom_records)

    elapsed = time.monotonic() - started
    rpt = Report("primary", plat.pretty_name, plat.backend_key)
    rpt.write(records, elapsed, notes)
    if not dry_run:
        state.mark_primary_completed()
    return records


def customization_flow(inst, catalog, plat, console, dry_run):
    console.print()
    console.print("Customization", style=theme.HEADING)
    custom = catalog.get("customization", {})
    editors = custom.get("editors", [])
    shells = custom.get("shells", {})
    notes = []

    selected = menu.editor_multiselect(editors)
    entries = []
    for editor in editors:
        if editor["name"] in selected:
            e = dict(editor)
            e["_category"] = "Customization"
            entries.append(e)

    theme_choice = menu.select(
        console,
        "Choose a shell prompt theme:",
        [("starship", "starship - lightweight, fast, no shell switch required"),
         ("oh-my-zsh", "oh-my-zsh - heavier, popular, requires switching to zsh")],
        default="starship",
    )

    if theme_choice == "starship" and "starship" in shells:
        entries.append(_shell_entry("starship", shells["starship"]))
    elif theme_choice == "oh-my-zsh" and "zsh" in shells:
        entries.append(_shell_entry("zsh", shells["zsh"]))

    want_fish = menu.confirm(console, "Install the fish shell as an optional extra?", default=False)
    if want_fish and "fish" in shells:
        entries.append(_shell_entry("fish", shells["fish"]))

    records = inst.install_sequence(entries) if entries else []

    if theme_choice == "oh-my-zsh":
        records.append(_install_oh_my_zsh(console, dry_run))
        if not dry_run and menu.confirm(console, "Switch default shell to zsh?", default=False):
            _switch_shell_to_zsh(console)

    default_editor = _choose_default_editor(selected)
    if default_editor and not dry_run:
        path = shellConfig.set_editor(default_editor, console=console)
        notes.append(f"Default EDITOR set to {default_editor} in {path}. "
                     f"Change it later with: python3 main.py --set-editor")
    elif default_editor:
        notes.append(f"Default EDITOR would be set to {default_editor} (dry-run).")

    return records, notes


def _shell_entry(name, spec):
    entry = dict(spec)
    entry["name"] = name
    entry["_category"] = "Customization"
    return entry


def _choose_default_editor(selected):
    if not selected:
        return None
    if "nano" in selected:
        return "nano"
    if "micro" in selected:
        return "micro"
    return selected[0]


def _install_oh_my_zsh(console, dry_run):
    url = "https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh"
    console.print(f"oh-my-zsh installs by running its official installer from {url}", style=theme.WARN)
    if dry_run:
        return PackageRecord("Customization", "oh-my-zsh", "Community zsh configuration framework.",
                             SKIPPED, result=OperationResult("oh-my-zsh", SKIPPED, stdout="dry-run"))
    if shutil.which("zsh") is None:
        result = OperationResult("oh-my-zsh", FAILED, stderr="zsh is not installed")
        return PackageRecord("Customization", "oh-my-zsh", "Community zsh configuration framework.",
                             FAILED, result=result, cause="zsh missing",
                             suggestion="Select oh-my-zsh again after zsh installs successfully.")
    # oh-my-zsh's installer rewrites ~/.zshrc; preserve the current one first.
    shellConfig.backup_dotfile(shellConfig.rc_path_for_shell("zsh"), console)
    command = f'sh -c "$(curl -fsSL {url})" "" --unattended'
    env = dict(os.environ, RUNZSH="no", CHSH="no")
    try:
        proc = subprocess.run(["bash", "-c", command], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=300, check=False)
        status = INSTALLED if proc.returncode == 0 else FAILED
        result = OperationResult("oh-my-zsh", status, ["bash", "-c", command],
                                 proc.stdout or "", proc.stderr or "", proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        result = OperationResult("oh-my-zsh", FAILED, stderr=str(exc))
    return PackageRecord("Customization", "oh-my-zsh", "Community zsh configuration framework.",
                         result.status, result=result)


def _switch_shell_to_zsh(console):
    zsh = shutil.which("zsh")
    if not zsh:
        console.print("zsh not found; cannot switch shell.", style=theme.WARN)
        return
    try:
        subprocess.run(["chsh", "-s", zsh], check=False)
        console.print(f"Default shell set to {zsh}. Restart your terminal to apply.", style=theme.DIM)
    except OSError as exc:
        console.print(f"Could not switch shell: {exc}", style=theme.WARN)


def run_secondary(inst, catalog, plat, console):
    console.print()
    menu.notice(console,
                "Secondary tools marked [LOCKED] require a proot Linux chroot or root access "
                "and may not fully function on stock or unrooted devices.",
                title="[ HEADS UP ]", border_style=theme.WARN)

    entries = []
    for block in catalog.get("secondary", []):
        for entry in block.get("packages", []):
            e = dict(entry)
            e["_category"] = block.get("category", "")
            entries.append(e)

    selected = menu.secondary_menu(entries)
    if not selected:
        console.print("No tools selected.", style=theme.DIM)
        return []

    chosen = [e for e in entries if e["name"] in selected]
    console.print()
    menu.notice(console, "You are about to install: " + ", ".join(e["name"] for e in chosen),
                title="[ CONFIRM ]", border_style=theme.ACCENT)
    if not menu.confirm(console, "Proceed with installation?", default=False):
        console.print("Cancelled.", style=theme.DIM)
        return []

    started = time.monotonic()
    records = inst.install_sequence(chosen)
    elapsed = time.monotonic() - started

    rpt = Report("secondary", plat.pretty_name, plat.backend_key)
    rpt.write(records, elapsed)
    return records


def banner_generation_flow(backend, plat, console, dry_run):
    if not menu.confirm(console, "Generate a persistent terminal banner?", default=False):
        return
    generator = BannerGenerator(backend, console)
    if not dry_run:
        generator.ensure_tools()

    name = menu.text(console, "Name to display:", default="hacker")
    handle = menu.text(console, "Handle or tagline (optional):", default="")
    text_value = f"{name} {handle}".strip() if handle else name

    if menu.confirm(console, "Customize font and color scheme?", default=False):
        font = menu.select(console, "Font:", [(f, f) for f in FONTS], default=FONTS[0])
        scheme = menu.select(console, "Color scheme:", [(c, c) for c in COLOR_SCHEMES], default=COLOR_SCHEMES[0])
    else:
        font, scheme = default_settings()

    if dry_run:
        console.print(f"Would render banner '{text_value}' with {font}/{scheme} and persist it.",
                      style=theme.DIM)
        return

    command = generator.render_preview(text_value, font, scheme)
    if command and menu.confirm(console, "Add this banner to your shell startup?", default=True):
        path = generator.persist(command)
        console.print(f"Banner added to {path}. It appears on your next shell start.", style=theme.DIM)


def action_report(console):
    content = report.read_reports()
    if content is None:
        console.print("No reports found. Run an install first to generate reports/.", style=theme.WARN)
        return
    # Reports are plain text with no markup; print without style interpretation.
    console.print(content, markup=False, highlight=False)


def action_remove_banner(console):
    removed_any = False
    for shell in ("bash", "zsh"):
        path, removed = shellConfig.remove_banner_block(shell, console=console)
        if removed:
            console.print(f"Removed banner block from {path}.", style=theme.DIM)
            removed_any = True
    if removed_any:
        console.print("Restart your terminal for the change to take effect.", style=theme.DIM)
    else:
        console.print("No banner block found.", style=theme.WARN)


def action_set_editor(catalog, console, dry_run):
    editors = catalog.get("customization", {}).get("editors", [])
    selected = menu.editor_multiselect(editors)
    editor = _choose_default_editor(selected)
    if not editor:
        console.print("No editor selected.", style=theme.WARN)
        return
    if dry_run:
        console.print(f"Would set EDITOR to {editor} (dry-run).", style=theme.DIM)
        return
    path = shellConfig.set_editor(editor, console=console)
    console.print(f"EDITOR set to {editor} in {path}. Restart your terminal to apply.", style=theme.DIM)


def finish_run(backend, plat, console, dry_run, no_banner):
    """Offer banner generation, then show the end banner. Shared by the flag paths."""
    banner_generation_flow(backend, plat, console, dry_run)
    if not no_banner:
        banner.show_end(VERSION, plat, console)


def run_menu(inst, catalog, plat, console, backend, dry_run):
    """Interactive main menu shown for a bare invocation. Returns an exit code."""
    end_shown = False
    while True:
        choice = menu.main_menu(console)
        if choice == "1":
            run_primary(inst, catalog, plat, console, dry_run, inst.verbose)
            end_shown = _show_end_once(plat, console, end_shown)
        elif choice == "2":
            end_shown = _menu_secondary(inst, catalog, plat, console, dry_run, end_shown)
        elif choice == "3":
            banner_generation_flow(backend, plat, console, dry_run)
        elif choice == "4":
            report_submenu(console)
        else:  # "5" or a cancelled prompt
            if not end_shown:
                banner.show_end(VERSION, plat, console)
            return 0


def _show_end_once(plat, console, end_shown):
    """Show the end banner only if it has not fired yet this session."""
    if not end_shown:
        banner.show_end(VERSION, plat, console)
    return True


def _menu_secondary(inst, catalog, plat, console, dry_run, end_shown):
    """Run Secondary from the menu, gated behind a completed Primary run."""
    if not state.primary_completed():
        console.print("Secondary Setup requires Primary Setup to be run first.", style=theme.WARN)
        if not menu.confirm(console, "Run Primary Setup now?", default=False):
            return end_shown
        run_primary(inst, catalog, plat, console, dry_run, inst.verbose)
    run_secondary(inst, catalog, plat, console)
    return _show_end_once(plat, console, end_shown)


def report_submenu(console):
    """Nested View Last Report menu; each report opens independently."""
    while True:
        choice = menu.report_menu(console)
        if choice == "4a":
            open_report(console, "primary")
        elif choice == "4b":
            open_report(console, "secondary")
        elif choice == "4c":
            open_report(console, "primary")
            open_report(console, "secondary")
        else:  # "4d" or a cancelled prompt
            return


# Exit-key hints shown before an editor launches so users are never trapped in it.
_EDITOR_HINTS = {
    "nano": "To exit nano: press Ctrl+X, then Y to save (or N to discard), then Enter.",
    "vim": "To exit vim: press Esc, type :wq, then Enter (or :q! to discard).",
    "vi": "To exit vi: press Esc, type :wq, then Enter (or :q! to discard).",
    "nvim": "To exit neovim: press Esc, type :wq, then Enter (or :q! to discard).",
    "micro": "To exit micro: press Ctrl+Q (press Ctrl+S first to save).",
    "emacs": "To exit emacs: press Ctrl+X then Ctrl+C (it will prompt to save).",
}
_EDITOR_HINT_DEFAULT = "Save and close the editor to return to the menu."


def open_report(console, kind):
    """Open a saved report in nano, falling back to the configured editor, then to a printed path."""
    path = report.report_path(kind)
    if not os.path.isfile(path):
        console.print(f"No {kind} report found yet — run {kind.capitalize()} Setup first.",
                      style=theme.WARN)
        return

    filename = os.path.basename(path)
    for editor in _report_editors():
        console.print(f"Opening {filename} in {_editor_name(editor)}...", style=theme.DIM)
        console.print(_editor_hint(editor), style=theme.DIM)
        if _launch_editor(editor, path):
            return
        console.print(f"Could not open {_editor_name(editor)}.", style=theme.WARN)

    console.print("No usable editor found. Open this file manually:", style=theme.WARN)
    console.print(os.path.abspath(path))


def _report_editors():
    """Editors to try, nano first, then the one set in Customization or the $EDITOR env var."""
    editors = []
    if shutil.which("nano"):
        editors.append("nano")
    configured = shellConfig.get_editor() or os.environ.get("EDITOR")
    if configured and _editor_name(configured) != "nano":
        editors.append(configured)
    return editors


def _editor_name(editor):
    # shlex.split raises on unbalanced quotes; fall back to a plain split so naming never crashes.
    try:
        parts = shlex.split(editor)
    except ValueError:
        parts = editor.split()
    return os.path.basename(parts[0]) if parts else editor


def _editor_hint(editor):
    return _EDITOR_HINTS.get(_editor_name(editor), _EDITOR_HINT_DEFAULT)


def _launch_editor(editor, path):
    """Run an interactive editor on path. Returns True if it launched, False if it could not."""
    try:
        subprocess.run(shlex.split(editor) + [path], check=False)
        return True
    except (OSError, ValueError):
        return False


def main(argv=None):
    args = build_parser().parse_args(argv)
    console = Console()

    if args.version:
        console.print(f"LinuxSetupPro v{VERSION}")
        return 0

    if args.report:
        action_report(console)
        return 0

    if args.remove_banner:
        action_remove_banner(console)
        return 0

    plat = resolve_platform(console)
    check_python_version(plat, console)

    if not args.no_banner:
        banner.show_startup(VERSION, console, plat, public_ip=args.public_ip)

    backend = get_backend(plat.backend_key, dry_run=args.dry_run)
    if backend is None:
        console.print("No package backend available for this platform.", style=theme.ERROR)
        return 1

    catalog = installer.load_catalog()

    if args.set_editor:
        action_set_editor(catalog, console, args.dry_run)
        return 0

    classifier = ErrorClassifier(plat.backend_key)
    inst = installer.Installer(backend, classifier, console, verbose=args.verbose)

    if args.only == "secondary":
        run_secondary(inst, catalog, plat, console)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    if args.only == "primary":
        run_primary(inst, catalog, plat, console, args.dry_run, args.verbose)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    # Any CLI flag keeps the direct pre-menu behavior; the menu is reserved for a bare invocation.
    raw_args = sys.argv[1:] if argv is None else argv
    if raw_args:
        run_primary(inst, catalog, plat, console, args.dry_run, args.verbose)
        if menu.confirm(console, "Proceed to the secondary security tools menu?", default=False):
            run_secondary(inst, catalog, plat, console)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    # Bare invocation: the interactive main menu.
    return run_menu(inst, catalog, plat, console, backend, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
