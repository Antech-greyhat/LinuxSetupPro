"""Writes append-only, plain-text installation reports under reports/."""

import os
from datetime import datetime

from core.packageManager import (
    ALREADY_INSTALLED,
    INSTALLED,
    INSTALLED_UNVERIFIED,
    UPGRADED,
    SKIPPED,
    FAILED,
)

_REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")

_FILENAMES = {
    "primary": "primary_installation.txt",
    "secondary": "secondary_installation.txt",
}

# Title-cased labels used in the Status column.
_STATUS_LABEL = {
    ALREADY_INSTALLED: "Already Installed",
    INSTALLED: "Installed",
    INSTALLED_UNVERIFIED: "Installed (Unverified)",
    UPGRADED: "Upgraded",
    SKIPPED: "Skipped",
    FAILED: "Failed",
}

# Order and wording of the per-status counts on the summary line.
_SUMMARY_ORDER = [INSTALLED, INSTALLED_UNVERIFIED, ALREADY_INSTALLED, UPGRADED, SKIPPED, FAILED]
_SUMMARY_LABEL = {
    INSTALLED: "installed",
    INSTALLED_UNVERIFIED: "installed (unverified)",
    ALREADY_INSTALLED: "already present",
    UPGRADED: "upgraded",
    SKIPPED: "skipped",
    FAILED: "failed",
}

_INSTALLED_STATES = (INSTALLED, INSTALLED_UNVERIFIED, ALREADY_INSTALLED, UPGRADED)

_INDENT = "  "
_GAP = "  "
_WIDTH = 60
_DESC_DASHES = 26


class PackageRecord:
    """One package's outcome, carrying enough detail to render its report row."""

    def __init__(self, category, name, description, status, locked=False,
                 result=None, cause=None, suggestion=None, available=True):
        self.category = category
        self.name = name
        self.description = description
        self.status = status
        self.locked = locked
        self.result = result
        self.cause = cause
        self.suggestion = suggestion
        self.available = available


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_report_dir():
    os.makedirs(_REPORT_DIR, exist_ok=True)


def _group_by_category(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record.category, []).append(record)
    return grouped


def _status_label(status):
    return _STATUS_LABEL.get(status, status.title())


class Report:
    def __init__(self, kind, platform_name, backend_key):
        self.kind = kind  # "primary" or "secondary"
        self.platform_name = platform_name
        self.backend_key = backend_key

    def path(self):
        return os.path.join(_REPORT_DIR, _FILENAMES.get(self.kind, f"{self.kind}.txt"))

    def write(self, records, elapsed, notes=None):
        """Append this run's dated block to the report file. Returns the file path."""
        _ensure_report_dir()
        path = self.path()
        block = self._render_block(records, elapsed, notes)
        prefix = "\n" if os.path.isfile(path) and os.path.getsize(path) > 0 else ""
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(prefix + block)
        return path

    def _render_block(self, records, elapsed, notes):
        pkg_w = max([len("Package")] + [len(r.name) for r in records]) if records else len("Package")
        status_w = max([len("Status")] + [len(_status_label(r.status)) for r in records]) \
            if records else len("Status")
        sub_indent = " " * (len(_INDENT) + pkg_w + len(_GAP))

        lines = ["=" * _WIDTH,
                 f" {self.kind.upper()} INSTALLATION REPORT",
                 f" Run: {_timestamp()}",
                 "=" * _WIDTH,
                 ""]

        for category, items in _group_by_category(records).items():
            lines.append(category.upper())
            lines.append("-" * _WIDTH)
            lines.append(_INDENT + "Package".ljust(pkg_w) + _GAP + "Status".ljust(status_w) + _GAP + "Description")
            lines.append(_INDENT + "-" * pkg_w + _GAP + "-" * status_w + _GAP + "-" * _DESC_DASHES)
            for record in items:
                lines.extend(self._render_row(record, pkg_w, status_w, sub_indent))
            lines.append("")

        if notes:
            lines.append("NOTES")
            lines.append("-" * _WIDTH)
            for note in notes:
                lines.append(_INDENT + note)
            lines.append("")

        lines.append("-" * _WIDTH)
        lines.append(f" SUMMARY: {self._summary(records)}")
        lines.append(f" Duration: {int(round(elapsed))}s")
        lines.append("=" * _WIDTH)
        return "\n".join(lines) + "\n"

    def _render_row(self, record, pkg_w, status_w, sub_indent):
        rows = [_INDENT + record.name.ljust(pkg_w) + _GAP
                + _status_label(record.status).ljust(status_w) + _GAP + record.description]

        if record.status == FAILED:
            reason, suggestion = self._failure_detail(record)
            rows.append(sub_indent + f"Reason: {reason}")
            rows.append(sub_indent + f"Suggestion: {suggestion}")

        # Locked tools carry a persistent chroot/root reminder once installed.
        if record.locked and record.status in _INSTALLED_STATES:
            rows.append(sub_indent + "Note: Requires a proot chroot or root access to function.")

        return rows

    def _failure_detail(self, record):
        if record.cause:
            return record.cause, record.suggestion or ""
        tail = (record.result.stderr.strip().splitlines() if record.result and record.result.stderr else [])
        reason = tail[-1] if tail else "unrecognized error"
        return reason, "Re-run with --verbose to see full output."

    def _summary(self, records):
        counts = {}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        parts = [f"{counts[s]} {_SUMMARY_LABEL[s]}" for s in _SUMMARY_ORDER if counts.get(s)]
        return " | ".join(parts) if parts else "nothing to do"


def report_path(kind):
    """Absolute path of a report file by kind ('primary' or 'secondary'), existent or not."""
    return os.path.join(_REPORT_DIR, _FILENAMES[kind])


def read_reports():
    """Return the combined text of the saved report files, or None if none exist."""
    chunks = []
    for kind in ("primary", "secondary"):
        path = os.path.join(_REPORT_DIR, _FILENAMES[kind])
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                chunks.append(fh.read())
    if not chunks:
        return None
    return "\n".join(chunks)
