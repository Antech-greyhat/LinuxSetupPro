"""Tracks cross-run state markers under .state/."""

import json
import os
from datetime import datetime

_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".state")
_PRIMARY_MARKER = os.path.join(_STATE_DIR, "primary_completed")
# Packages this tool actually installed, so they can later be uninstalled.
_INSTALLED_MANIFEST = os.path.join(_STATE_DIR, "installed_packages.json")
# Where a Primary/Secondary run got to, so an interrupted run can resume.
_PROGRESS_MARKER = os.path.join(_STATE_DIR, "in_progress.json")


def mark_primary_completed():
    """Record that a Primary run finished. Best-effort; never raises."""
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_PRIMARY_MARKER, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
    except OSError:
        pass


def primary_completed():
    """True if a Primary run has completed at least once."""
    return os.path.isfile(_PRIMARY_MARKER)


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path, payload):
    """Write JSON via a temp file then rename, so a killed run never leaves a torn file."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


# --- Tool-installed package manifest (drives uninstall) ---

def _load_manifest():
    """Read the installed-package manifest as a list. A missing or corrupt file reads as empty."""
    try:
        with open(_INSTALLED_MANIFEST, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def record_installed(backend_key, record):
    """Record one package this tool installed, so it becomes an uninstall candidate.

    Deduped by (backend, name) so re-runs never double-list a package. Best-effort.
    """
    package = record.result.package if record.result else record.name
    entry = {
        "name": record.name,
        "package": package,
        "category": record.category,
        "description": record.description,
        "locked": bool(record.locked),
        "backend": backend_key,
        "installed_at": _timestamp(),
    }
    manifest = [e for e in _load_manifest()
                if not (e.get("backend") == backend_key and e.get("name") == record.name)]
    manifest.append(entry)
    try:
        _atomic_write_json(_INSTALLED_MANIFEST, manifest)
    except OSError:
        pass


def list_installed(backend_key=None):
    """Tool-installed packages, optionally filtered to one backend, in the order recorded."""
    manifest = _load_manifest()
    if backend_key is None:
        return manifest
    return [e for e in manifest if e.get("backend") == backend_key]


def forget_installed(backend_key, name):
    """Drop a package from the manifest once it is no longer installed by the tool. Best-effort."""
    manifest = [e for e in _load_manifest()
                if not (e.get("backend") == backend_key and e.get("name") == name)]
    try:
        _atomic_write_json(_INSTALLED_MANIFEST, manifest)
    except OSError:
        pass


# --- In-progress run marker (drives resume) ---

def write_progress(run_type, queue, position, category):
    """Record how far a run has got: its type, the category in progress, and the completed count.

    queue is the ordered list of entry dicts the run installs; position is how many are done.
    Rewritten after each package so an interrupted run resumes where it stopped. Best-effort.
    """
    payload = {
        "run": run_type,
        "category": category,
        "position": position,
        "queue": queue,
        "updated_at": _timestamp(),
    }
    try:
        _atomic_write_json(_PROGRESS_MARKER, payload)
    except (OSError, TypeError):
        pass


def read_run():
    """Return the in-progress run marker, or None if there is none or it is unreadable."""
    try:
        with open(_PROGRESS_MARKER, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "run" not in data:
        return None
    return data


def clear_run():
    """Remove the in-progress run marker once a run ends or is declined. Best-effort."""
    try:
        os.remove(_PROGRESS_MARKER)
    except OSError:
        pass
