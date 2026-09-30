"""LinuxSetupPro entry point: CLI parsing and full install orchestration."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import namedtuple

from rich.console import Console

from core import detector, doctor, installer, profile as profile_mod, report, shellConfig, state, updateCheck
from core.bannerGenerator import BannerGenerator, FONTS, COLOR_SCHEMES, default_settings
from core.errorClassifier import ErrorClassifier
from core.packageManager import (
    get_backend,
    OperationResult,
    INSTALLED,
    FAILED,
    SKIPPED,
    REMOVED,
    FAILED_REMOVE,
    NOT_FOUND,
)
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
    parser.add_argument("--skip", metavar="CATEGORIES",
                        help="comma-separated categories to exclude from the run")
    parser.add_argument("--categories", metavar="CATEGORIES",
                        help="comma-separated categories to run exclusively (mutually exclusive with --skip)")
    parser.add_argument("--export-profile", metavar="FILE",
                        help="write the current tracked selections to FILE as a profile, then exit")
    parser.add_argument("--import-profile", metavar="FILE",
                        help="install exactly the selections in profile FILE without prompts, then exit")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove packages this tool installed, then exit (skips the menu)")
    parser.add_argument("--set-editor", action="store_true",
                        help="reopen the editor picker, update EDITOR, then exit")
    parser.add_argument("--report", action="store_true",
                        help="print the saved installation reports and exit")
    parser.add_argument("--check-update", action="store_true",
                        help="check GitHub for a newer release, print the result, and exit")
    parser.add_argument("--doctor", action="store_true",
                        help="health-check tracked packages against the system, then exit")
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


# One package to install, carrying the coordinates its progress line is numbered by.
_Step = namedtuple("_Step", "entry category index total")


def _primary_queue(catalog):
    """Ordered list of primary entries, each tagged with its category."""
    return [entry for _, entry in installer.flatten_primary(catalog)]


def _steps_from_queue(queue, per_category):
    """Turn an install queue into numbered steps.

    per_category numbers each package within its category (Primary's convention); otherwise
    packages are numbered across the whole queue (Secondary's convention). Both are rebuilt the
    same way on resume, so the numbering a run shows survives an interruption.
    """
    steps = []
    if per_category:
        index = 0
        while index < len(queue):
            category = queue[index].get("_category", "")
            run = index
            while run < len(queue) and queue[run].get("_category", "") == category:
                run += 1
            size = run - index
            for offset in range(size):
                steps.append(_Step(queue[index + offset], category, offset + 1, size))
            index = run
    else:
        total = len(queue)
        for i, entry in enumerate(queue):
            steps.append(_Step(entry, entry.get("_category", ""), i + 1, total))
    return steps


def _install_steps(inst, run_type, queue, steps, console, start_pos=0, show_headings=True):
    """Install steps[start_pos:], recording progress before each package so a kill can resume.

    A category heading prints as the category changes when show_headings is set. The marker is
    left untouched in dry-run, which is not a resumable operation.
    """
    track = not inst.backend.dry_run
    records = []
    current_category = None
    for position in range(start_pos, len(steps)):
        step = steps[position]
        if show_headings and step.category != current_category:
            current_category = step.category
            console.print()
            console.print(step.category, style=theme.HEADING)
        if track:
            # Written before the package runs, so a package killed mid-install is retried on resume.
            state.write_progress(run_type, queue, position, step.category)
        records.extend(inst.install_sequence([step.entry], start_index=step.index, total=step.total))
    if track and steps:
        state.write_progress(run_type, queue, len(steps), steps[-1].category)
    return records


def _write_primary_report(plat, dry_run, records, notes, started):
    """Write the primary report and clear the run marker. Shared by the interactive and silent paths."""
    elapsed = time.monotonic() - started
    Report("primary", plat.pretty_name, plat.backend_key).write(records, elapsed, notes)
    if not dry_run:
        state.mark_primary_completed()
    state.clear_run()
    return records


def _record_primary_selection(ran_categories, run_customization, selection):
    """Persist the primary selections a run made, so a profile can be exported later. Best-effort."""
    payload = {"primary_categories": [installer.category_slug(name) for name in ran_categories]}
    if run_customization:
        payload["primary_categories"].append(installer.CUSTOMIZATION)
        if selection is not None:
            payload["customization"] = {
                "editors": list(selection.get("editors", [])),
                "shell_theme": selection.get("shell_theme"),
                "install_fish": bool(selection.get("install_fish", False)),
            }
    state.merge_selections(payload)


def _finalize_primary(inst, catalog, plat, console, dry_run, records, started,
                      ran_categories, run_customization):
    """Run the customization tail when selected, record selections, write the report, clear the marker."""
    if run_customization:
        custom_records, notes, selection = customization_flow(inst, catalog, plat, console, dry_run)
        records.extend(custom_records)
    else:
        notes, selection = [], None
    _record_primary_selection(ran_categories, run_customization, selection)
    return _write_primary_report(plat, dry_run, records, notes, started)


def run_primary(inst, catalog, plat, console, dry_run, verbose, filt):
    allowed = filt.primary
    queue = [entry for entry in _primary_queue(catalog) if entry.get("_category") in allowed]
    steps = _steps_from_queue(queue, per_category=True)
    started = time.monotonic()
    records = _install_steps(inst, "primary", queue, steps, console, show_headings=True)
    ran = [name for name in installer.primary_category_names(catalog)
           if name in allowed and name != installer.CUSTOMIZATION]
    return _finalize_primary(inst, catalog, plat, console, dry_run, records, started,
                             ran, installer.CUSTOMIZATION in allowed)


def customization_flow(inst, catalog, plat, console, dry_run):
    """Interactive customization tail: gather editor, theme, and fish choices, then apply them.

    Returns (records, notes, selection), where selection captures the choices for profile export.
    """
    console.print()
    console.print("Customization", style=theme.HEADING)
    editors = catalog.get("customization", {}).get("editors", [])

    selected = menu.editor_multiselect(editors)
    theme_choice = menu.select(
        console,
        "Choose a shell prompt theme:",
        [("starship", "starship - lightweight, fast, no shell switch required"),
         ("oh-my-zsh", "oh-my-zsh - heavier, popular, requires switching to zsh")],
        default="starship",
    )
    want_fish = menu.confirm(console, "Install the fish shell as an optional extra?", default=False)

    records, notes = apply_customization(inst, catalog, console, dry_run,
                                         selected, theme_choice, want_fish, interactive=True)
    selection = {"editors": selected, "shell_theme": theme_choice, "install_fish": want_fish}
    return records, notes, selection


def apply_customization(inst, catalog, console, dry_run, selected, theme_choice, want_fish, interactive):
    """Install the customization selections. Skips every prompt when interactive is False.

    Shared by the interactive tail and profile import; the only interactive-only step is the
    optional switch of the default shell to zsh after an oh-my-zsh install.
    """
    custom = catalog.get("customization", {})
    editors = custom.get("editors", [])
    shells = custom.get("shells", {})
    notes = []

    entries = []
    for editor in editors:
        if editor["name"] in selected:
            e = dict(editor)
            e["_category"] = "Customization"
            entries.append(e)

    if theme_choice == "starship" and "starship" in shells:
        entries.append(_shell_entry("starship", shells["starship"]))
    elif theme_choice == "oh-my-zsh" and "zsh" in shells:
        entries.append(_shell_entry("zsh", shells["zsh"]))

    if want_fish and "fish" in shells:
        entries.append(_shell_entry("fish", shells["fish"]))

    records = inst.install_sequence(entries) if entries else []

    if theme_choice == "oh-my-zsh":
        records.append(_install_oh_my_zsh(console, dry_run))
        if interactive and not dry_run and menu.confirm(console, "Switch default shell to zsh?", default=False):
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


def _finalize_secondary(plat, records, started):
    """Write the secondary report and clear the run marker."""
    elapsed = time.monotonic() - started
    Report("secondary", plat.pretty_name, plat.backend_key).write(records, elapsed)
    state.clear_run()
    return records


def run_secondary(inst, catalog, plat, console, filt):
    console.print()
    menu.notice(console,
                "Secondary tools marked [LOCKED] require a proot Linux chroot or root access "
                "and may not fully function on stock or unrooted devices.",
                title="[ HEADS UP ]", border_style=theme.WARN)

    entries = [entry for _, entry in installer.flatten_secondary(catalog)
               if entry.get("_category") in filt.secondary]
    if not entries:
        console.print("No secondary categories selected.", style=theme.DIM)
        return []

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

    state.merge_selections({"secondary_selections": [e["name"] for e in chosen]})
    steps = _steps_from_queue(chosen, per_category=False)
    started = time.monotonic()
    records = _install_steps(inst, "secondary", chosen, steps, console, show_headings=False)
    return _finalize_secondary(plat, records, started)


def resume_run(inst, catalog, plat, console, dry_run, marker):
    """Continue an interrupted run from the position its marker recorded, then finalize as usual."""
    run_type = marker.get("run")
    queue = marker.get("queue") or []
    position = max(0, int(marker.get("position", 0)))
    started = time.monotonic()
    if run_type == "primary":
        steps = _steps_from_queue(queue, per_category=True)
        records = _install_steps(inst, "primary", queue, steps, console,
                                 start_pos=position, show_headings=True)
        # The interrupted run's categories are whatever its saved queue held; customization always
        # ran as the tail before, so it is offered again on resume.
        ran = list(dict.fromkeys(entry.get("_category", "") for entry in queue if entry.get("_category")))
        _finalize_primary(inst, catalog, plat, console, dry_run, records, started, ran, True)
    elif run_type == "secondary":
        steps = _steps_from_queue(queue, per_category=False)
        records = _install_steps(inst, "secondary", queue, steps, console,
                                 start_pos=position, show_headings=False)
        state.merge_selections({"secondary_selections": [entry.get("name") for entry in queue]})
        _finalize_secondary(plat, records, started)
    else:
        # An unrecognized run type is not something we can safely replay; drop the marker.
        state.clear_run()


def _maybe_resume(inst, catalog, plat, console, dry_run):
    """Offer to resume an interrupted run before the main menu; declining discards the marker."""
    marker = state.read_run()
    if not marker or not menu.is_interactive():
        return
    category = marker.get("category") or "a previous run"
    if menu.confirm(console, f"Previous run was interrupted during {category}. Resume?", default=False):
        resume_run(inst, catalog, plat, console, dry_run, marker)
    else:
        state.clear_run()


def run_uninstall(inst, plat, console):
    """Remove tool-installed packages the user selects, then report the outcome per package."""
    candidates = state.list_installed(plat.backend_key)
    if not candidates:
        console.print("No tool-installed packages to uninstall.", style=theme.WARN)
        return []

    chosen = menu.uninstall_menu(candidates)
    if not chosen:
        console.print("No packages selected.", style=theme.DIM)
        return []

    console.print()
    menu.notice(console, "You are about to remove: " + ", ".join(c["name"] for c in chosen),
                title="[ CONFIRM ]", border_style=theme.ACCENT)
    if not menu.confirm(console, "Proceed with removal?", default=False):
        console.print("Cancelled.", style=theme.DIM)
        return []

    started = time.monotonic()
    records = []
    total = len(chosen)
    for index, candidate in enumerate(chosen, start=1):
        record = _remove_one(inst, candidate, index, total)
        # A package that is gone (removed, or never present) is no longer tool-installed.
        if record.status in (REMOVED, NOT_FOUND):
            state.forget_installed(plat.backend_key, candidate["name"])
        records.append(record)
    elapsed = time.monotonic() - started

    Report("uninstall", plat.pretty_name, plat.backend_key).write(records, elapsed)
    return records


def _remove_one(inst, candidate, index, total):
    """Remove one package through its backend, mapping the backend result to an uninstall status."""
    backend = inst.backend
    package = candidate["package"]

    def operation():
        # Nothing to do if the package is already gone; report it plainly, not as a failure.
        if not backend.dry_run and not backend.is_installed(package):
            return OperationResult(package, NOT_FOUND)
        result = backend.remove(package)
        if result.status == INSTALLED:      # backends signal a successful removal with INSTALLED
            result.status = REMOVED
        elif result.status == FAILED:
            result.status = FAILED_REMOVE
        return result

    if inst.verbose:
        inst.console.print(f"[{index}/{total}] removing {candidate['name']} ({package})")
        result = operation()
        if result.stdout:
            inst.console.print(result.stdout)
        if result.stderr:
            inst.console.print(result.stderr)
        inst.console.print(f"  -> {result.status}")
    else:
        result = inst.progress.run(index, total, "remove", candidate["name"], operation)

    return PackageRecord(candidate.get("category", ""), candidate["name"],
                         candidate.get("description", ""), result.status,
                         bool(candidate.get("locked")), result)


def _has_any_selection(selections):
    """True if tracked state holds something a profile could be built from."""
    return bool(selections and (selections.get("primary_categories")
                                or selections.get("customization")
                                or selections.get("secondary_selections")))


def action_export_profile(catalog, console, path):
    """Write the tracked selections to path as a profile. Returns an exit code."""
    selections = state.read_selections()
    if not _has_any_selection(selections):
        console.print("No tracked selections to export yet. Run Primary or Secondary Setup first.",
                      style=theme.ERROR)
        return 1
    try:
        profile = profile_mod.from_selections(catalog, selections)
        profile_mod.export_profile(profile, path)
    except (profile_mod.ProfileError, OSError) as exc:
        console.print(f"Could not export profile: {exc}", style=theme.ERROR)
        return 1
    console.print(f"Profile written to {path}.", style=theme.DIM)
    return 0


def _silent_primary(inst, catalog, plat, console, dry_run, profile):
    """Install a profile's primary categories and customization with no selection prompts."""
    allowed = set(profile.primary_categories)
    queue = [entry for entry in _primary_queue(catalog) if entry.get("_category") in allowed]
    steps = _steps_from_queue(queue, per_category=True)
    started = time.monotonic()
    records = _install_steps(inst, "primary", queue, steps, console, show_headings=True)
    if profile.run_customization:
        console.print()
        console.print("Customization", style=theme.HEADING)
        custom = profile.customization
        custom_records, notes = apply_customization(inst, catalog, console, dry_run, custom.editors,
                                                    custom.shell_theme, custom.install_fish, interactive=False)
        records.extend(custom_records)
    else:
        notes = []
    return _write_primary_report(plat, dry_run, records, notes, started)


def _silent_secondary(inst, catalog, plat, console, profile):
    """Install a profile's secondary tool selections with no checkbox menu."""
    by_name = {entry["name"]: entry for _, entry in installer.flatten_secondary(catalog)}
    chosen = [by_name[name] for name in profile.secondary_selections if name in by_name]
    if not chosen:
        return []
    steps = _steps_from_queue(chosen, per_category=False)
    started = time.monotonic()
    records = _install_steps(inst, "secondary", chosen, steps, console, show_headings=False)
    return _finalize_secondary(plat, records, started)


def run_import_profile(inst, catalog, plat, console, dry_run, profile):
    """Install exactly what a validated profile specifies. Per-package progress and reports show normally."""
    records = []
    if profile.primary_categories or profile.run_customization:
        records.extend(_silent_primary(inst, catalog, plat, console, dry_run, profile))
    if profile.secondary_selections:
        records.extend(_silent_secondary(inst, catalog, plat, console, profile))
    if not records:
        console.print("The profile selected nothing to install.", style=theme.WARN)
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


def action_check_update(console):
    """Query GitHub for a newer release and report the outcome in a panel. Never crashes on network failure."""
    result = updateCheck.check_for_update(VERSION)
    if result.status == updateCheck.UPDATE_AVAILABLE:
        menu.notice(console,
                    f"A newer version ({result.latest}) is available.\n"
                    f"You are currently on v{result.current}.\n\n"
                    "This tool never auto-updates, so your local changes and "
                    "customizations stay intact. Open the release page below to review "
                    "the changes and download it.",
                    title="[ UPDATE AVAILABLE ]", border_style=theme.ACCENT)
        console.print(result.url or updateCheck.RELEASES_PAGE, style=theme.ACCENT)
    elif result.status == updateCheck.UP_TO_DATE:
        menu.notice(console, f"You are on the latest version (v{result.current}).",
                    title="[ UP TO DATE ]", border_style=theme.ACCENT)
    else:
        menu.notice(console,
                    "Could not check for updates — the network may be unreachable. "
                    "Nothing else is affected; try again later.",
                    title="[ UPDATE CHECK FAILED ]", border_style=theme.WARN)


def action_doctor(backend, plat, console):
    """Audit tracked tool-installed packages, write the health report, and print a short summary."""
    result = doctor.run_health_check(backend, plat.backend_key)
    path = report.write_health_report(result.checked, result.discrepancies)
    summary = report.health_summary_line(len(result.checked), len(result.discrepancies))
    console.print()
    menu.notice(console, summary, title="[ HEALTH CHECK ]",
                border_style=theme.WARN if result.discrepancies else theme.ACCENT)
    console.print(f"Full report: {os.path.relpath(path)}", style=theme.DIM)


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


def run_menu(inst, catalog, plat, console, backend, dry_run, filt):
    """Interactive main menu shown for a bare invocation. Returns an exit code."""
    end_shown = False
    while True:
        choice = menu.main_menu(console)
        if choice == "1":
            run_primary(inst, catalog, plat, console, dry_run, inst.verbose, filt)
            end_shown = _show_end_once(plat, console, end_shown)
        elif choice == "2":
            end_shown = _menu_secondary(inst, catalog, plat, console, dry_run, filt, end_shown)
        elif choice == "3":
            banner_generation_flow(backend, plat, console, dry_run)
        elif choice == "4":
            report_submenu(console)
        elif choice == "5":
            run_uninstall(inst, plat, console)
        elif choice == "6":
            action_check_update(console)
        elif choice == "7":
            action_doctor(backend, plat, console)
        elif choice == "8":
            menu_export_profile(catalog, console)
        elif choice == "9":
            if menu_import_profile(inst, catalog, plat, console, dry_run):
                end_shown = _show_end_once(plat, console, end_shown)
        else:  # "10" or a cancelled prompt
            if not end_shown:
                banner.show_end(VERSION, plat, console)
            return 0


def menu_export_profile(catalog, console):
    """Prompt for a path and write the tracked selections there as a profile."""
    if not _has_any_selection(state.read_selections()):
        console.print("No tracked selections yet. Run Primary or Secondary Setup first.", style=theme.WARN)
        return
    path = menu.text(console, "Path to write the profile to:", default="profile.yaml")
    action_export_profile(catalog, console, path)


def menu_import_profile(inst, catalog, plat, console, dry_run):
    """Prompt for a profile path and silently install it. Returns True if an install ran."""
    path = menu.text(console, "Path to the profile to import:", default="")
    if not path:
        console.print("No path given.", style=theme.DIM)
        return False
    try:
        profile = profile_mod.load_profile(path, catalog)
    except profile_mod.ProfileError as exc:
        console.print(f"Cannot import profile: {exc}", style=theme.ERROR)
        return False
    run_import_profile(inst, catalog, plat, console, dry_run, profile)
    return True


def _show_end_once(plat, console, end_shown):
    """Show the end banner only if it has not fired yet this session."""
    if not end_shown:
        banner.show_end(VERSION, plat, console)
    return True


def _menu_secondary(inst, catalog, plat, console, dry_run, filt, end_shown):
    """Run Secondary from the menu, gated behind a completed Primary run."""
    if not state.primary_completed():
        console.print("Secondary Setup requires Primary Setup to be run first.", style=theme.WARN)
        if not menu.confirm(console, "Run Primary Setup now?", default=False):
            return end_shown
        run_primary(inst, catalog, plat, console, dry_run, inst.verbose, filt)
    run_secondary(inst, catalog, plat, console, filt)
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


def _split_categories(value):
    """Split a comma-separated --skip/--categories value into trimmed, non-empty tokens."""
    if not value:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def main(argv=None):
    args = build_parser().parse_args(argv)
    console = Console()

    if args.version:
        console.print(f"LinuxSetupPro v{VERSION}")
        return 0

    # --skip/--categories refine a normal install run; they cannot pair with each other or with a
    # profile action, which already fixes exactly what runs. Reject those combinations up front.
    if args.skip and args.categories:
        console.print("Use only one of --skip or --categories, not both.", style=theme.ERROR)
        return 1
    if (args.skip or args.categories) and (args.export_profile or args.import_profile):
        console.print("--skip/--categories cannot be combined with --export-profile or --import-profile.",
                      style=theme.ERROR)
        return 1

    if args.report:
        action_report(console)
        return 0

    if args.check_update:
        action_check_update(console)
        return 0

    if args.remove_banner:
        action_remove_banner(console)
        return 0

    if args.export_profile:
        # Serializing tracked selections needs no platform, so this works anywhere a run was recorded.
        return action_export_profile(installer.load_catalog(), console, args.export_profile)

    plat = resolve_platform(console)
    check_python_version(plat, console)

    if not args.no_banner:
        banner.show_startup(VERSION, console, plat, public_ip=args.public_ip)

    backend = get_backend(plat.backend_key, dry_run=args.dry_run)
    if backend is None:
        console.print("No package backend available for this platform.", style=theme.ERROR)
        return 1

    if args.doctor:
        action_doctor(backend, plat, console)
        return 0

    catalog = installer.load_catalog()

    if args.set_editor:
        action_set_editor(catalog, console, args.dry_run)
        return 0

    try:
        filt = installer.resolve_filter(catalog, skip=_split_categories(args.skip),
                                        categories=_split_categories(args.categories))
    except installer.CategoryError as exc:
        console.print(str(exc), style=theme.ERROR)
        return 1

    classifier = ErrorClassifier(plat.backend_key)
    inst = installer.Installer(backend, classifier, console, verbose=args.verbose)

    if args.import_profile:
        try:
            profile = profile_mod.load_profile(args.import_profile, catalog)
        except profile_mod.ProfileError as exc:
            console.print(f"Cannot import profile: {exc}", style=theme.ERROR)
            return 1
        run_import_profile(inst, catalog, plat, console, args.dry_run, profile)
        if not args.no_banner:
            banner.show_end(VERSION, plat, console)
        return 0

    if args.uninstall:
        run_uninstall(inst, plat, console)
        if not args.no_banner:
            banner.show_end(VERSION, plat, console)
        return 0

    if args.only == "secondary":
        run_secondary(inst, catalog, plat, console, filt)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    if args.only == "primary":
        run_primary(inst, catalog, plat, console, args.dry_run, args.verbose, filt)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    # Any CLI flag keeps the direct pre-menu behavior; the menu is reserved for a bare invocation.
    raw_args = sys.argv[1:] if argv is None else argv
    if raw_args:
        run_primary(inst, catalog, plat, console, args.dry_run, args.verbose, filt)
        if menu.confirm(console, "Proceed to the secondary security tools menu?", default=False):
            run_secondary(inst, catalog, plat, console, filt)
        finish_run(backend, plat, console, args.dry_run, args.no_banner)
        return 0

    # Bare invocation: offer to resume any interrupted run, then show the interactive main menu.
    _maybe_resume(inst, catalog, plat, console, args.dry_run)
    return run_menu(inst, catalog, plat, console, backend, args.dry_run, filt)


if __name__ == "__main__":
    sys.exit(main())
