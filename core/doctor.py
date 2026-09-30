"""Health check: audits tracked tool-installed packages against what is on the system."""

import os
import shutil
import subprocess
import tempfile
from collections import namedtuple

from core import state

# One audited package: its label, backend name, category, description, and whether it is still present.
PackageStatus = namedtuple("PackageStatus", "name package category description present")

# Outcome of the C toolchain smoke test: the compiler tried, whether it compiled and ran, and the
# captured error detail when it did not.
ToolchainResult = namedtuple("ToolchainResult", "compiler ok detail")

# Result of a health check: every package audited, the subset found missing, and the C toolchain
# check (None when no C compiler is tracked, so there is nothing to test).
HealthReport = namedtuple("HealthReport", "checked discrepancies toolchain")

# A trivial program that must compile and exit zero for the toolchain to count as working.
_C_PROBE = "int main() { return 0; }\n"
# Compiling and running an empty program is quick; cap it so a wedged toolchain cannot stall the check.
_TOOLCHAIN_TIMEOUT = 30


def run_health_check(backend, backend_key):
    """Re-check every tracked tool-installed package against the backend. Returns a HealthReport.

    Only packages this tool installed are recorded in the manifest, so pre-existing
    ('already installed') packages are never audited or flagged as drift. A tracked C compiler is
    additionally smoke-tested end to end.
    """
    checked = []
    for entry in state.list_installed(backend_key):
        package = entry.get("package") or entry.get("name", "")
        checked.append(PackageStatus(
            entry.get("name", package), package, entry.get("category", ""),
            entry.get("description", ""), _is_present(backend, package)))
    discrepancies = [status for status in checked if not status.present]
    return HealthReport(checked, discrepancies, _check_c_toolchain(backend_key))


def _is_present(backend, package):
    """True if the backend still reports package installed. A check that errors is not treated as drift."""
    try:
        return bool(backend.is_installed(package))
    except Exception:
        # An unexpected backend error must not turn an unverifiable package into a false discrepancy.
        return True


def _tracked_compiler(backend_key):
    """The C compiler to smoke-test: clang when tracked, else gcc, else None. Prefers clang."""
    tracked = {entry.get("name") for entry in state.list_installed(backend_key)}
    if "clang" in tracked:
        return "clang"
    if "gcc" in tracked:
        return "gcc"
    return None


def _check_c_toolchain(backend_key):
    """Compile and run a trivial C program with the tracked compiler. None when none is tracked.

    Confirms the toolchain works end to end rather than trusting the package manager's success.
    Temp files are always cleaned up, and any subprocess failure is captured, never raised.
    """
    compiler = _tracked_compiler(backend_key)
    if compiler is None:
        return None
    try:
        workdir = tempfile.mkdtemp(prefix="lsp-cc-")
    except OSError as exc:
        return ToolchainResult(compiler, False, str(exc))
    try:
        source = os.path.join(workdir, "probe.c")
        binary = os.path.join(workdir, "probe.bin")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write(_C_PROBE)
        code, _, err = _run([compiler, source, "-o", binary])
        if code != 0:
            return ToolchainResult(compiler, False, _detail(err, code))
        code, _, err = _run([binary])
        if code != 0:
            return ToolchainResult(compiler, False, _detail(err, code))
        return ToolchainResult(compiler, True, "")
    except OSError as exc:
        # Writing the probe or creating the output path failed; report it instead of crashing.
        return ToolchainResult(compiler, False, str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run(command):
    """Run command capturing output; returns (exit_code, stdout, stderr). Never raises.

    Mirrors the defensive subprocess handling used elsewhere: a missing binary, timeout, or OS
    error resolves to a non-zero code and a message rather than an exception.
    """
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=_TOOLCHAIN_TIMEOUT, check=False)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {_TOOLCHAIN_TIMEOUT}s"
    except OSError as exc:
        return 1, "", str(exc)


def _detail(stderr, code):
    """A short, actionable failure detail: the captured stderr, or the exit code when it is empty."""
    text = stderr.strip()
    return text if text else f"exited with code {code}"
