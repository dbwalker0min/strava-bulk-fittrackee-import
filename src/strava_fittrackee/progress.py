"""Rich presentation for typed importer progress; disabled for redirected output."""

from __future__ import annotations

import sys
from types import TracebackType

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .models import Outcome, Stage


class RichReporter:
    def __init__(
        self, *, dryrun: bool, enabled: bool = True, console: Console | None = None
    ) -> None:
        console = console if console is not None else Console(stderr=True)
        self.enabled = enabled and console.is_terminal and sys.stdout.isatty()
        self.label = "Dry run" if dryrun else "Import"
        self.display = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("{task.fields[stage]}", markup=False),
            console=console,
            disable=not self.enabled,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
            refresh_per_second=4,
        )
        self.task = self.display.add_task(self.label, total=None, stage="preparing")
        self.completed = 0
        self.total: int | None = None

    def __enter__(self) -> RichReporter:
        self.display.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            finished = exc_type is None and self.total is not None and self.completed == self.total
            self.display.update(
                self.task,
                description=self.label,
                stage="finished" if finished else "stopped",
                refresh=True,
            )
        finally:
            self.display.stop()

    def start(self, total: int) -> None:
        self.total = total
        self.display.update(self.task, total=total, completed=0, stage="selecting", refresh=True)

    def activity(self, external_id: str, stage: Stage) -> None:
        self.display.update(
            self.task, description=f"{self.label} {external_id}", stage=stage, refresh=True
        )

    def advance(self, outcome: Outcome) -> None:
        self.completed += 1
        self.display.update(self.task, completed=self.completed, stage=outcome, refresh=True)

    def emit(self, message: str) -> None:
        if self.enabled:
            self.display.console.print(message, markup=False, highlight=False, soft_wrap=True)
        else:
            print(message, flush=True)
