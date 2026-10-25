from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from strava_fittrackee import cli

runner = CliRunner()


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["--help"],
        ["import", "--help"],
        ["login", "--help"],
        ["refresh", "--help"],
        ["recover", "--help"],
    ],
)
def test_command_help_does_not_create_state(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, command)
    assert result.exit_code == (2 if not command else 0)
    assert "Usage:" in result.output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flag", ["--dryrun", "--dry-run"])
def test_import_preserves_flags_and_opaque_repeated_ids(monkeypatch, flag):
    captured = []
    monkeypatch.setattr(cli, "execute", lambda options: captured.append(options) or 0)
    result = runner.invoke(
        cli.app,
        [
            "import",
            "synthetic.zip",
            flag,
            "--no-progress",
            "--no-photos",
            "--include-indoor",
            "--all-sports",
            "--limit",
            "2",
            "--activity-id",
            "001",
            "--activity-id",
            "002",
            "--sport-map",
            "sports.json",
            "--timezone",
            "UTC",
            "--state-dir",
            "private-state",
        ],
    )
    assert result.exit_code == 0, result.output
    options = captured[0]
    assert options.dryrun and options.no_progress and options.no_photos
    assert options.include_indoor and options.all_sports
    assert options.activity_id == ["001", "002"] and options.limit == 2
    assert options.state_dir == Path("private-state") and options.sport_map == Path("sports.json")
    assert options.timezone == "UTC"


@pytest.mark.parametrize(
    "arguments",
    [
        ["recover", "--activity-id", "001"],
        ["recover", "--activity-id", "001", "--workout-id", "native", "--confirm-no-upload"],
        ["login"],
        ["import", "file.zip", "--dryrun", "--limit", "0"],
        ["login", "--client-id", "client", "--port", "0"],
        ["login", "--client-id", "client", "--port", "65536"],
    ],
)
def test_invalid_arguments_rejected_before_credentials_or_state(tmp_path, arguments):
    state = tmp_path / "private-state"
    result = runner.invoke(
        cli.app, [*arguments, "--server", "https://fit.example", "--state-dir", str(state)]
    )
    assert result.exit_code == 2, result.output
    assert not state.exists()


def test_server_is_required_for_online_commands():
    for command in ["login", "refresh", "recover"]:
        result = runner.invoke(cli.app, [command])
        assert result.exit_code == 2
        assert "--server" in Text.from_ansi(result.output).plain


def test_typer_preserves_blocked_exit_status(archive):
    archive.activities[0].row["Filename"] = "missing.gpx"
    # Rewrite via a fixture to ensure the CLI reads the changed row from the ZIP.
    from .conftest import make_archive

    folder = Path(archive.zip.filename).parent
    source = make_archive(folder, [archive.activities[0].row], {})
    result = runner.invoke(cli.app, ["import", str(source.zip.filename), "--dryrun"])
    assert result.exit_code == 1
    assert "blocked" in result.output
    source.close()


def test_errors_do_not_render_locals_or_secrets(monkeypatch):
    def fail(options):
        raise ValueError("private-secret")

    monkeypatch.setattr(cli, "execute", fail)
    result = runner.invoke(cli.app, ["import", "file.zip", "--dryrun"])
    assert result.exit_code == 2
    assert "private-secret" not in result.output and "Traceback" not in result.output


def test_keyboard_interrupt_keeps_existing_exit_code(monkeypatch):
    def interrupt(options):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "execute", interrupt)
    result = runner.invoke(cli.app, ["import", "file.zip", "--dryrun"])
    assert result.exit_code == 130
    assert "journal preserves recovery state" in result.output


@pytest.mark.parametrize("command", ["login", "refresh"])
def test_oauth_commands_dispatch_without_changing_secret_handling(command, monkeypatch):
    captured = []
    monkeypatch.setattr(cli, "execute", lambda options: captured.append(options) or 0)
    extra = ["--client-id", "client", "--port", "9123"] if command == "login" else []
    result = runner.invoke(cli.app, [command, "--server", "https://fit.example", *extra])
    assert result.exit_code == 0, result.output
    assert captured[0].command == command
    assert captured[0].server == "https://fit.example"
    if command == "login":
        assert captured[0].client_id == "client" and captured[0].port == 9123
