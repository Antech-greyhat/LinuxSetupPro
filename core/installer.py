"""Drives install sequences using the selected backend. Holds no platform commands."""

import os
import re
import shlex
import subprocess
from collections import namedtuple

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
from core import state

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
            record = self._install_one(entry, index, total)
            # Only packages this run actually installed become uninstall candidates.
            if record.status in (INSTALLED, INSTALLED_UNVERIFIED):
                state.record_installed(self.backend.key, record)
            records.append(record)
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


# Pseudo-category naming the interactive customization tail, so --skip/--categories can gate it
# alongside the real primary categories even though it is not one of the primary catalog blocks.
CUSTOMIZATION = "customization"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class CategoryError(ValueError):
    """A --skip/--categories token names no known category, or is ambiguous."""


# Which categories a run installs, resolved from --skip/--categories. Each field is a frozenset of
# catalog category names (primary also carries the CUSTOMIZATION pseudo-category); empty means none.
CategoryFilter = namedtuple("CategoryFilter", "primary secondary")


def category_slug(name):
    """Canonical comparable slug for a category name: lowercased, non-alphanumeric runs to '-'."""
    return _SLUG_RE.sub("-", str(name).strip().lower()).strip("-")


def primary_category_names(catalog):
    """Ordered primary category names, plus the trailing customization pseudo-category when present."""
    names = [block.get("category", "") for block in catalog.get("primary", [])]
    names = [n for n in names if n]
    if catalog.get("customization"):
        names.append(CUSTOMIZATION)
    return names


def secondary_category_names(catalog):
    """Ordered secondary category names as they appear in the catalog."""
    return [n for n in (block.get("category", "") for block in catalog.get("secondary", [])) if n]


def all_category_names(catalog):
    """Every selectable category name: primary (with customization), then secondary, in order."""
    return primary_category_names(catalog) + secondary_category_names(catalog)


def resolve_category_tokens(catalog, tokens, names=None):
    """Map user category tokens to catalog category names, matched by slug.

    A token matches by exact slug or as a whole leading token (so 'fun' matches 'Fun/Terminal
    Flair'). Raises CategoryError, listing the valid slugs, on a token that matches none or more
    than one. names limits the candidate set (e.g. primary-only when validating a profile).
    """
    candidates = names if names is not None else all_category_names(catalog)
    slugged = [(category_slug(name), name) for name in candidates]
    matched = set()
    unknown = []
    for token in tokens:
        needle = category_slug(token)
        if not needle:
            continue
        hits = [name for slug, name in slugged if slug == needle or slug.startswith(needle + "-")]
        if len(hits) == 1:
            matched.add(hits[0])
        elif not hits:
            unknown.append(token)
        else:
            raise CategoryError(f"Category '{token}' is ambiguous; it matches: "
                                + ", ".join(category_slug(h) for h in hits))
    if unknown:
        valid = ", ".join(category_slug(name) for name in candidates)
        label = "category" if len(unknown) == 1 else "categories"
        listed = ", ".join(repr(u) for u in unknown)
        raise CategoryError(f"Unknown {label} {listed}. Valid categories: {valid}")
    return matched


def resolve_filter(catalog, skip=None, categories=None):
    """Turn --skip/--categories token lists into a CategoryFilter. Raises CategoryError on bad input.

    With neither, everything runs. --categories is a positive selection (only the named run);
    --skip is negative (everything but the named). The two are mutually exclusive.
    """
    primary = primary_category_names(catalog)
    secondary = secondary_category_names(catalog)
    if skip and categories:
        raise CategoryError("Use only one of --skip or --categories, not both.")
    if categories:
        chosen = resolve_category_tokens(catalog, categories)
        return CategoryFilter(frozenset(n for n in primary if n in chosen),
                              frozenset(n for n in secondary if n in chosen))
    if skip:
        skipped = resolve_category_tokens(catalog, skip)
        return CategoryFilter(frozenset(n for n in primary if n not in skipped),
                              frozenset(n for n in secondary if n not in skipped))
    return CategoryFilter(frozenset(primary), frozenset(secondary))
