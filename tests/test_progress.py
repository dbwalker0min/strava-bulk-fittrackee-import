from __future__ import annotations

import io
import re
import sys

import pytest
from rich.console import Console
from typer.testing import CliRunner

from strava_fittrackee.cli import app
from strava_fittrackee.client import APIError
from strava_fittrackee.importer import run
from strava_fittrackee.journal import Journal
from strava_fittrackee.models import Outcome, Stage
from strava_fittrackee.progress import RichReporter

from .conftest import make_archive


class RecordedProgress:
    def __init__(self):
        self.total = None
        self.stages = []
        self.outcomes = []

    def start(self, total: int) -> None:
        self.total = total

    def activity(self, external_id: str, stage: Stage) -> None:
        self.stages.append((external_id, stage))

    def advance(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


def inspect(archive, reporter, **kwargs):
    return run(
        archive,
        client=None,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
        progress=reporter,
        **kwargs,
    )


def test_progress_counts_candidates_exclusions_and_blockers_once(tmp_path, archive):
    row = archive.activities[0].row
    source = make_archive(
        tmp_path,
        [
            row,
            {**row, "Activity ID": "002", "Activity Type": "Walk"},
            {**row, "Activity ID": "003", "Filename": "missing.gpx"},
        ],
    )
    reporter = RecordedProgress()
    counts = inspect(source, reporter)
    assert reporter.total == 3
    assert reporter.outcomes == ["candidate", "excluded", "blocked"]
    assert sum(counts.values()) == 3
    assert ("001", "preparing files") in reporter.stages
    source.close()


def test_progress_total_respects_selection_and_limit(tmp_path, archive):
    row = archive.activities[0].row
    source = make_archive(
        tmp_path, [row, {**row, "Activity ID": "002"}, {**row, "Activity ID": "003"}]
    )
    reporter = RecordedProgress()
    run(
        source,
        client=None,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=1,
        activity_ids={"002", "003"},
        emit=lambda _: None,
        progress=reporter,
    )
    assert reporter.total == 1
    assert reporter.outcomes == ["candidate"]
    assert all(key == "002" for key, _ in reporter.stages)
    source.close()


def test_import_and_identity_skip_advance_without_second_upload(client, server, archive, tmp_path):
    journal = Journal(tmp_path / "journal.db", "test")
    reporter = RecordedProgress()
    for _ in range(2):
        run(
            archive,
            client=client,
            journal=journal,
            dryrun=False,
            timezone="UTC",
            overrides={},
            include_photos=True,
            limit=None,
            activity_ids=set(),
            emit=lambda _: None,
            progress=reporter,
        )
    assert reporter.outcomes == ["imported", "skipped"]
    assert ("001", "claiming") in reporter.stages
    assert ("001", "uploading workout") in reporter.stages
    assert ("001", "binding identity") in reporter.stages
    assert server.workouts == 1
    journal.close()


def test_api_stop_does_not_complete_unprocessed_rows(client, archive, tmp_path, monkeypatch):
    row = archive.activities[0].row
    source = make_archive(tmp_path, [row, {**row, "Activity ID": "002"}])
    original = client.request

    def request(method, path, **kwargs):
        if "/identities/" in path:
            raise APIError(401, "request_rejected")
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    reporter = RecordedProgress()
    counts = run(
        source,
        client=client,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
        progress=reporter,
    )
    assert reporter.total == 2
    assert reporter.outcomes == ["blocked"]
    assert counts["blocked"] == 1
    source.close()


def test_interruption_does_not_advance_current_activity(archive, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(archive, "recording", interrupt)
    reporter = RecordedProgress()
    with pytest.raises(KeyboardInterrupt):
        inspect(archive, reporter)
    assert reporter.total == 1
    assert reporter.outcomes == []


def terminal_reporter(monkeypatch, *, enabled=True):
    stream = io.StringIO()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    console = Console(
        file=stream, force_terminal=True, color_system=None, width=140, legacy_windows=False
    )
    return RichReporter(dryrun=True, enabled=enabled, console=console), stream


def plain(text):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def test_rich_terminal_shows_count_and_current_stage(monkeypatch):
    reporter, stream = terminal_reporter(monkeypatch)
    with reporter:
        reporter.start(2)
        reporter.activity("001", "preparing files")
        reporter.advance("candidate")
        reporter.activity("002", "checking identity")
        reporter.advance("skipped")
    output = plain(stream.getvalue())
    assert "Dry run 001" in output and "preparing files" in output
    assert "checking identity" in output and "finished" in output
    assert "2/2" in output and "100%" in output
    assert not reporter.display.live.is_started


def test_rich_early_stop_is_not_reported_as_complete(monkeypatch):
    reporter, stream = terminal_reporter(monkeypatch)
    with reporter:
        reporter.start(2)
        reporter.advance("blocked")
    output = plain(stream.getvalue())
    assert "1/2" in output and "stopped" in output
    assert "100%" not in output


def test_rich_stops_after_exception(monkeypatch):
    reporter, stream = terminal_reporter(monkeypatch)
    with pytest.raises(KeyboardInterrupt), reporter:
        reporter.start(2)
        reporter.advance("candidate")
        raise KeyboardInterrupt()
    assert "stopped" in plain(stream.getvalue())
    assert not reporter.display.live.is_started


def test_redirected_output_is_plain_and_literal(monkeypatch, capsys):
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    reporter = RichReporter(dryrun=True, console=console)
    with reporter:
        reporter.start(1)
        reporter.emit("001: blocked ([bold]literal text[/bold])")
        reporter.advance("blocked")
    assert not reporter.enabled
    assert stream.getvalue() == ""
    assert capsys.readouterr().out == "001: blocked ([bold]literal text[/bold])\n"


def test_no_progress_overrides_terminal(monkeypatch, capsys):
    reporter, stream = terminal_reporter(monkeypatch, enabled=False)
    with reporter:
        reporter.start(1)
        reporter.emit("001: candidate")
        reporter.advance("candidate")
    assert stream.getvalue() == ""
    assert capsys.readouterr().out == "001: candidate\n"


def test_cli_progress_switch_keeps_dryrun_readonly(tmp_path, archive, capsys):
    state = tmp_path / "no-state"
    result = CliRunner().invoke(
        app,
        [
            "import",
            str(archive.zip.filename),
            "--dryrun",
            "--no-progress",
            "--state-dir",
            str(state),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "candidate" in result.output and "Summary:" in result.output
    assert "\x1b[" not in result.output
    assert not state.exists()


def test_progress_live_dryrun_has_no_mutations(client, server, archive):
    client.readonly = True
    reporter = RecordedProgress()
    run(
        archive,
        client=client,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
        progress=reporter,
    )
    assert reporter.outcomes == ["would_import"]
    assert all(method == "GET" for method, _ in server.calls)
    assert server.workouts == 0 and server.media == 0 and server.token is None
