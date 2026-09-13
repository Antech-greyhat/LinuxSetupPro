"""Drives install sequences using the selected backend. Holds no platform commands."""

import os
import shlex
import subprocess

import yaml

from core.packageManager import (
    OperationResult,
    INSTALLED,
    INSTALLED_UNVERIFIED,
    SKIPPED,
    FAILED,
)
from core.progress import ProgressRenderer
from core.report import PackageRecord

_CATALOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "packages.yaml")

# A verify_cmd must answer quickly; a hung check must never stall the whole run.
_VERIFY_TIMEOUT = 5


def load_catalog(path=_CATALOG):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_package(entry, backend_key):
    """Return the backend-specific package name, or None if unavailable there."""
    return entry.get(backend_key)


class Installer:
    def __init__(self, backend, classifier, console, verbose=False):
        self.backend = backend
        self.classifier = classifier
        self.console = console
        self.verbose = verbose
        self.progress = ProgressRenderer(console)

    def install_sequence(self, entries, start_index=1, total=None):
        """Install each entry in order. Returns a list of PackageRecord."""
        total = total if total is not None else len(entries)
        records = []
        for offset, entry in enumerate(entries):
            index = start_index + offset
            records.append(self._install_one(entry, index, total))
        return records

    def _install_one(self, entry, index, total):
        name = entry["name"]
        description = entry.get("description", "")
        locked = bool(entry.get("locked", False))
        pkg = resolve_package(entry, self.backend.key)

        if pkg is None:
            reason = f"not packaged for the {self.backend.key} backend"
            result = OperationResult(name, SKIPPED, stdout=reason)
            self._echo_unavailable(index, total, name)
            return PackageRecord(entry.get("_category", ""), name, description,
                                 SKIPPED, locked, result,
                                 cause=reason,
                                 suggestion="Install it from a Linux chroot (proot-distro) or a language package manager.",
                                 available=False)

        verify_cmd = entry.get("verify_cmd")
        result = self._run_install(index, total, name, pkg, verify_cmd)
        record = PackageRecord(entry.get("_category", ""), name, description,
                               result.status, locked, result)
        if result.status == FAILED:
            classified = self.classifier.classify(result.stderr)
            if classified:
                record.cause, record.suggestion = classified
        return record

    def _run_install(self, index, total, name, pkg, verify_cmd=None):
        operation = lambda: self._install_and_verify(pkg, verify_cmd)
        if self.verbose:
            self.console.print(f"[{index}/{total}] installing {name} ({pkg})")
            result = operation()
            if result.stdout:
                self.console.print(result.stdout)
            if result.stderr:
                self.console.print(result.stderr)
            self.console.print(f"  -> {result.status}")
            return result
        return self.progress.run(index, total, "install", name, operation)

    def _install_and_verify(self, pkg, verify_cmd):
        """Install pkg, then confirm it actually runs when a verify_cmd is configured."""
        result = self.backend.install(pkg)
        # Only a fresh install is checked; already-present, upgraded, and dry-run states are left as-is.
        if verify_cmd and result.status == INSTALLED and not self.backend.dry_run:
            if not self._verify(verify_cmd):
                result.status = INSTALLED_UNVERIFIED
        return result

    def _verify(self, verify_cmd):
        """Run a verify_cmd defensively; True only on a clean exit-zero response. Never raises."""
        try:
            args = shlex.split(verify_cmd)
        except ValueError:
            return False
        if not args:
            return False
        try:
            proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=_VERIFY_TIMEOUT, check=False)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _echo_unavailable(self, index, total, name):
        self.console.print(f"[{index}/{total}] {name} skipped (not available on this platform)")


def flatten_primary(catalog):
    """Yield (category, entry) for primary categories in defined order."""
    for block in catalog.get("primary", []):
        category = block.get("category", "")
        for entry in block.get("packages", []):
            entry = dict(entry)
            entry["_category"] = category
            yield category, entry


def flatten_secondary(catalog):
    for block in catalog.get("secondary", []):
        category = block.get("category", "")
        for entry in block.get("packages", []):
            entry = dict(entry)
            entry["_category"] = category
            yield category, entry
