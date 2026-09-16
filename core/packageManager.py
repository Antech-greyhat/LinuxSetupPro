"""Package manager abstraction. All platform-specific commands live here."""

import os
import shutil
import subprocess
import time

# Operation outcomes. Every backend method resolves to exactly one of these.
ALREADY_INSTALLED = "already installed"
INSTALLED = "installed"
UPGRADED = "upgraded"
SKIPPED = "skipped"
FAILED = "failed"
# Assigned by the installer (never by a backend) when a post-install verify_cmd does not confirm the tool.
INSTALLED_UNVERIFIED = "installed (unverified)"
# Uninstall outcomes, assigned by the uninstall flow from a backend remove() result.
REMOVED = "removed"
FAILED_REMOVE = "failed to remove"
NOT_FOUND = "not found"


class OperationResult:
    """Outcome of a single package operation. Backends never raise to the caller."""

    def __init__(self, package, status, command=None, stdout="", stderr="",
                 exit_code=None, duration=0.0):
        self.package = package
        self.status = status
        self.command = command or []
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.duration = duration

    @property
    def ok(self):
        return self.status in (ALREADY_INSTALLED, INSTALLED, UPGRADED)

    def as_dict(self):
        return {
            "package": self.package,
            "status": self.status,
            "command": " ".join(self.command) if self.command else "",
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration": round(self.duration, 3),
        }


def _run(command, timeout=1800):
    """Run a command, capturing everything. Returns (exit_code, stdout, stderr, duration)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.monotonic() - start
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}", time.monotonic() - start
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s", time.monotonic() - start
    except OSError as exc:
        return 1, "", f"failed to execute command: {exc}", time.monotonic() - start


class PackageManagerBackend:
    """Interface for a distro package manager. Concrete backends implement the four verbs."""

    key = None

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def _needs_sudo(self):
        return hasattr(os, "geteuid") and os.geteuid() != 0

    def _sudo_prefix(self):
        if self._needs_sudo() and shutil.which("sudo"):
            return ["sudo"]
        return []

    def is_installed(self, package):
        raise NotImplementedError

    def install(self, package):
        raise NotImplementedError

    def upgrade(self, package):
        raise NotImplementedError

    def remove(self, package):
        raise NotImplementedError

    def _dry(self, package, command):
        return OperationResult(package, SKIPPED, command=command,
                               stdout="dry-run: not executed", exit_code=0)


class TermuxBackend(PackageManagerBackend):
    key = "termux"

    def _sudo_prefix(self):
        return []  # Termux runs unprivileged; pkg needs no sudo.

    def is_installed(self, package):
        code, out, _, _ = _run(["dpkg-query", "-W", "-f=${Status}", package])
        return code == 0 and "install ok installed" in out

    def install(self, package):
        if self.is_installed(package):
            return OperationResult(package, ALREADY_INSTALLED)
        cmd = ["pkg", "install", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def upgrade(self, package):
        cmd = ["pkg", "install", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = UPGRADED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def remove(self, package):
        cmd = ["pkg", "uninstall", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED  # removal success reuses ok-state semantics upstream
        return OperationResult(package, status, cmd, out, err, code, dur)


class AptBackend(PackageManagerBackend):
    key = "apt"

    def is_installed(self, package):
        code, out, _, _ = _run(["dpkg-query", "-W", "-f=${Status}", package])
        return code == 0 and "install ok installed" in out

    def install(self, package):
        if self.is_installed(package):
            return OperationResult(package, ALREADY_INSTALLED)
        cmd = self._sudo_prefix() + ["apt-get", "install", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        env_code, _, env_err, _ = self._ensure_index()
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def _ensure_index(self):
        # apt refuses installs against a missing index; refresh once is cheap and idempotent.
        return _run(self._sudo_prefix() + ["apt-get", "update"])

    def upgrade(self, package):
        cmd = self._sudo_prefix() + ["apt-get", "install", "-y", "--only-upgrade", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = UPGRADED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def remove(self, package):
        cmd = self._sudo_prefix() + ["apt-get", "remove", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)


class DnfBackend(PackageManagerBackend):
    key = "dnf"

    def is_installed(self, package):
        code, _, _, _ = _run(["rpm", "-q", package])
        return code == 0

    def install(self, package):
        if self.is_installed(package):
            return OperationResult(package, ALREADY_INSTALLED)
        cmd = self._sudo_prefix() + ["dnf", "install", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def upgrade(self, package):
        cmd = self._sudo_prefix() + ["dnf", "upgrade", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = UPGRADED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def remove(self, package):
        cmd = self._sudo_prefix() + ["dnf", "remove", "-y", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)


class PacmanBackend(PackageManagerBackend):
    key = "pacman"

    def is_installed(self, package):
        code, _, _, _ = _run(["pacman", "-Q", package])
        return code == 0

    def install(self, package):
        if self.is_installed(package):
            return OperationResult(package, ALREADY_INSTALLED)
        cmd = self._sudo_prefix() + ["pacman", "-S", "--noconfirm", "--needed", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def upgrade(self, package):
        cmd = self._sudo_prefix() + ["pacman", "-S", "--noconfirm", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = UPGRADED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)

    def remove(self, package):
        cmd = self._sudo_prefix() + ["pacman", "-R", "--noconfirm", package]
        if self.dry_run:
            return self._dry(package, cmd)
        code, out, err, dur = _run(cmd)
        status = INSTALLED if code == 0 else FAILED
        return OperationResult(package, status, cmd, out, err, code, dur)


_BACKENDS = {
    "termux": TermuxBackend,
    "apt": AptBackend,
    "dnf": DnfBackend,
    "pacman": PacmanBackend,
}


def get_backend(backend_key, dry_run=False):
    cls = _BACKENDS.get(backend_key)
    if cls is None:
        return None
    return cls(dry_run=dry_run)
