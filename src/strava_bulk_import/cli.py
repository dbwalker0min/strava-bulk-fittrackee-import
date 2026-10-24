"""Command line interface for archive inspection, login, import and explicit recovery."""

from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import typer

from .archive import Archive
from .auth import login
from .client import Client, origin
from .errors import ImportProblem
from .importer import PREFIX, bind, profile, release_confirmed, run
from .journal import Journal, RunLock
from .models import object_value, string_value
from .progress import RichReporter

app = typer.Typer(
    help="Import outdoor cycling history from a Strava export into FitTrackee.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

Server: TypeAlias = Annotated[str, typer.Option("--server", help="FitTrackee HTTPS origin")]
StateDirectory: TypeAlias = Annotated[
    Path, typer.Option("--state-dir", help="Private credentials and recovery journal directory")
]
DEFAULT_STATE = Path.home() / ".strava-fittrackee"


@dataclass(frozen=True, kw_only=True)
class CommandOptions:
    """Validated Typer values passed to the command executor."""

    command: Literal["import", "login", "refresh", "recover"]
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE)
    server: str | None = None
    archive: Path | None = None
    client_id: str = ""
    port: int = 8765
    dryrun: bool = False
    no_progress: bool = False
    timezone: str | None = None
    sport_map: Path | None = None
    no_photos: bool = False
    include_indoor: bool = False
    all_sports: bool = False
    limit: int | None = None
    activity_id: list[str] = field(default_factory=list)
    recovery_activity_id: str = ""
    workout_id: str | None = None
    confirm_no_upload: bool = False


@app.command("import")
def import_archive(
    archive: Annotated[Path, typer.Argument(help="Strava account export ZIP")],
    server: Annotated[str | None, typer.Option("--server", help="FitTrackee HTTPS origin")] = None,
    state_dir: StateDirectory = DEFAULT_STATE,
    dryrun: Annotated[
        bool, typer.Option("--dryrun", "--dry-run", help="Inspect without mutations")
    ] = False,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="Disable terminal progress")
    ] = False,
    timezone: Annotated[
        str | None, typer.Option("--timezone", help="Offline target timezone")
    ] = None,
    sport_map: Annotated[
        Path | None, typer.Option("--sport-map", help="JSON sport label mappings")
    ] = None,
    no_photos: Annotated[bool, typer.Option("--no-photos", help="Omit photo attachments")] = False,
    include_indoor: Annotated[
        bool, typer.Option("--include-indoor", help="Include indoor/virtual activities")
    ] = False,
    all_sports: Annotated[
        bool, typer.Option("--all-sports", help="Include non-cycling sports")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Process the first N selected rows")
    ] = None,
    activity_id: Annotated[
        list[str] | None, typer.Option("--activity-id", help="Select an ID; repeat to select more")
    ] = None,
) -> None:
    """Inspect or import an archive; outdoor electric/non-electric cycling is the default."""
    invoke(
        CommandOptions(
            command="import",
            archive=archive,
            server=server,
            state_dir=state_dir,
            dryrun=dryrun,
            no_progress=no_progress,
            timezone=timezone,
            sport_map=sport_map,
            no_photos=no_photos,
            include_indoor=include_indoor,
            all_sports=all_sports,
            limit=limit,
            activity_id=activity_id or [],
        )
    )


@app.command("login")
def login_command(
    server: Server,
    client_id: Annotated[
        str, typer.Option("--client-id", help="Registered FitTrackee OAuth client ID")
    ],
    state_dir: StateDirectory = DEFAULT_STATE,
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="Loopback callback port")
    ] = 8765,
) -> None:
    """Authorize in a browser; the client secret is prompted privately."""
    invoke(
        CommandOptions(
            command="login", server=server, client_id=client_id, state_dir=state_dir, port=port
        )
    )


@app.command("refresh")
def refresh_command(server: Server, state_dir: StateDirectory = DEFAULT_STATE) -> None:
    """Refresh saved OAuth credentials without importing activities."""
    invoke(CommandOptions(command="refresh", server=server, state_dir=state_dir))


@app.command("recover")
def recover_command(
    server: Server,
    activity_id: Annotated[str, typer.Option("--activity-id", help="Exact Strava activity ID")],
    state_dir: StateDirectory = DEFAULT_STATE,
    workout_id: Annotated[
        str | None, typer.Option("--workout-id", help="Verified existing native workout ID")
    ] = None,
    confirm_no_upload: Annotated[
        bool,
        typer.Option(
            "--confirm-no-upload", help="Confirm no earlier upload exists or can complete"
        ),
    ] = False,
) -> None:
    """Bind a verified workout, or explicitly release a reconciled claim."""
    if (workout_id is not None) == confirm_no_upload:
        raise typer.BadParameter("Choose exactly one of --workout-id or --confirm-no-upload")
    invoke(
        CommandOptions(
            command="recover",
            server=server,
            state_dir=state_dir,
            recovery_activity_id=activity_id,
            workout_id=workout_id,
            confirm_no_upload=confirm_no_upload,
        )
    )


def execute(args: CommandOptions) -> int:
    credentials = args.state_dir / "credentials.json"
    client, journal, archive, lock = None, None, None, None
    try:
        if args.command == "login":
            assert args.server is not None
            lock = RunLock(args.state_dir)
            login(args.server, credentials, args.client_id, args.port)
            return 0
        readonly = args.command == "import" and args.dryrun
        if args.command == "import":
            if args.limit is not None and args.limit <= 0:
                raise ImportProblem("--limit must be positive")
            if not args.dryrun and not args.server:
                raise ImportProblem(
                    "--server is required for import; use --dryrun for offline inspection"
                )
            if args.server and args.timezone:
                raise ImportProblem(
                    "Online imports use the target profile timezone; omit --timezone"
                )
            assert args.archive is not None
            archive = Archive(args.archive)
        timezone = args.timezone or "UTC"
        ZoneInfo(timezone)
        overrides: dict[str, str] = {}
        if args.sport_map:
            overrides = {
                key: string_value(value)
                for key, value in object_value(json.loads(args.sport_map.read_text())).items()
            }
        if args.server:
            server = origin(args.server)
            if not readonly:
                lock = RunLock(args.state_dir)
            client = Client(server, credentials, readonly=readonly)
            if args.command == "refresh":
                client.refresh()
                print("Credentials refreshed.")
                return 0
            namespace, timezone = profile(client)
            ZoneInfo(timezone)
            if not readonly:
                journal = Journal(args.state_dir / "journal.sqlite3", namespace)
        if args.command == "recover":
            assert journal is not None and client is not None
            entry = journal.get(args.recovery_activity_id)
            if not entry:
                raise ImportProblem("No local claim journal exists for this account/activity")
            if args.confirm_no_upload:
                release_confirmed(client, journal, args.recovery_activity_id)
                print("Claim released. Inspect orphan media before retrying the activity.")
                return 0
            assert args.workout_id is not None
            if entry.get("workout_id") and entry["workout_id"] != args.workout_id:
                raise ImportProblem("Workout ID conflicts with the journal")
            # Registration checks actual native ownership, even for public workouts.
            client.request(
                "GET",
                PREFIX + "/workouts/" + quote(args.workout_id, safe="") + "/identities",
            )
            entry["workout_id"] = args.workout_id
            journal.save(args.recovery_activity_id, entry)
            bind(client, journal, args.recovery_activity_id, entry)
            print("Identity bound to the supplied workout. Future imports skip it.")
            return 0
        assert archive is not None
        if not client:
            print("Offline dryrun: identities and target sport availability are unverified.")
            print("Manual date preview timezone: " + timezone)
        with RichReporter(dryrun=args.dryrun, enabled=not args.no_progress) as reporter:
            counts = run(
                archive,
                client=client,
                journal=journal,
                dryrun=args.dryrun,
                timezone=timezone,
                overrides=overrides,
                include_photos=not args.no_photos,
                include_indoor=args.include_indoor,
                all_sports=args.all_sports,
                limit=args.limit,
                activity_ids=set(args.activity_id),
                emit=reporter.emit,
                progress=reporter,
            )
        print("Summary: " + json.dumps(dict(sorted(counts.items()))))
        return 1 if counts["blocked"] else 0
    finally:
        for resource in (archive, journal, client, lock):
            if resource:
                resource.close()


def invoke(args: CommandOptions) -> None:
    try:
        code = execute(args)
    except (
        ImportProblem,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
        sqlite3.Error,
        ZoneInfoNotFoundError,
        httpx.HTTPError,
    ) as exc:
        # Avoid printing exception bodies from HTTP libraries/JSON that may contain secrets.
        print(
            "Error: "
            + (
                str(exc)
                if isinstance(exc, ImportProblem)
                else "Invalid input or local/network failure; check configuration."
            ),
            file=sys.stderr,
        )
        code = 2
    except KeyboardInterrupt:
        print("Interrupted. The journal preserves recovery state.", file=sys.stderr)
        code = 130
    raise typer.Exit(code)


def main() -> None:
    app(prog_name="strava-fittrackee")
