"""Per-package progress rendering. Alternates a bar and a spinner by position."""

import threading

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.spinner import Spinner
from rich.live import Live
from rich.text import Text

from core.packageManager import OperationResult, FAILED
from ui import theme

# Verb shown while an operation runs, keyed by the operation kind.
VERBS = {
    "install": "installing",
    "upgrade": "upgrading",
    "remove": "removing",
}


class ProgressRenderer:
    def __init__(self, console=None):
        self.console = console or Console()

    def run(self, index, total, kind, package, operation):
        """Animate while operation() runs in a thread; return its OperationResult."""
        verb = VERBS.get(kind, kind)
        prefix = f"[{index}/{total}] {verb} {package}"
        result_box = {}

        def worker():
            try:
                result_box["result"] = operation()
            except Exception as exc:
                # An exception would otherwise die with the thread and leave no result to report;
                # one broken package must never abort the whole run.
                result_box["result"] = OperationResult(package, FAILED, stderr=str(exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        if index % 2 == 1:
            self._animate_bar(prefix, thread)
        else:
            self._animate_spinner(prefix, thread)

        thread.join()
        result = result_box.get("result")
        self._print_final(index, total, verb, package, result)
        return result

    def _animate_bar(self, prefix, thread):
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            console=self.console,
            transient=True,
        ) as progress:
            task = progress.add_task(prefix, total=None)
            while thread.is_alive():
                progress.advance(task, 1)
                thread.join(timeout=0.1)

    def _animate_spinner(self, prefix, thread):
        spinner = Spinner("dots", text=Text(prefix))
        with Live(spinner, console=self.console, transient=True, refresh_per_second=12):
            while thread.is_alive():
                thread.join(timeout=0.1)

    def _print_final(self, index, total, verb, package, result):
        status = result.status if result else "failed"
        style = theme.status_style(status)
        line = Text()
        line.append(f"[{index}/{total}] ", style=theme.DIM)
        line.append(f"{package} ", style="white")
        line.append(status, style=style)
        self.console.print(line)
