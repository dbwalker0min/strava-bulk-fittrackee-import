"""Command line interface for archive inspection, login, import and explicit recovery."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .archive import Archive, ImportProblem
from .auth import login
from .client import Client, origin
from .importer import PREFIX, bind, profile, release_confirmed, run
from .journal import Journal, RunLock


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="strava-fittrackee")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("import", "login", "refresh", "recover"):
        sub = commands.add_parser(name)
        sub.add_argument("--server", required=name != "import", help="FitTrackee HTTPS origin")
        sub.add_argument(
            "--state-dir",
            type=Path,
            default=Path.home() / ".strava-fittrackee",
            help="Private credentials and recovery journal directory",
        )
        if name == "login":
            sub.add_argument("--client-id", required=True)
            sub.add_argument("--port", type=int, default=8765)
        elif name == "import":
            sub.add_argument("archive", type=Path)
            sub.add_argument("--dryrun", "--dry-run", action="store_true")
            sub.add_argument(
                "--timezone", help="Offline dryrun target timezone; online uses profile"
            )
            sub.add_argument(
                "--sport-map", type=Path, help="JSON object: Strava type to FitTrackee label"
            )
            sub.add_argument("--no-photos", action="store_true")
            sub.add_argument(
                "--include-indoor",
                action="store_true",
                help="Include explicitly indoor/virtual activities (excluded by default)",
            )
            sub.add_argument(
                "--all-sports",
                action="store_true",
                help="Include non-cycling sports (cycling only by default)",
            )
            sub.add_argument("--limit", type=int)
            sub.add_argument("--activity-id", action="append", default=[])
        elif name == "recover":
            sub.add_argument("--activity-id", required=True)
            recovery = sub.add_mutually_exclusive_group(required=True)
            recovery.add_argument(
                "--workout-id",
                help="Verified native workout ID from the uncertain upload",
            )
            recovery.add_argument(
                "--confirm-no-upload",
                action="store_true",
                help="Release only after confirming no earlier upload can complete",
            )
    return root


def execute(args: argparse.Namespace) -> int:
    credentials = args.state_dir / "credentials.json"
    client, journal, archive, lock = None, None, None, None
    try:
        if args.command == "login":
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
            archive = Archive(args.archive)
        timezone = getattr(args, "timezone", None) or "UTC"
        ZoneInfo(timezone)
        overrides = {}
        if getattr(args, "sport_map", None):
            overrides = json.loads(args.sport_map.read_text())
            if not isinstance(overrides, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
            ):
                raise ImportProblem("Sport map must be a JSON object of string labels")
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
            entry = journal.get(args.activity_id)
            if not entry:
                raise ImportProblem("No local claim journal exists for this account/activity")
            if args.confirm_no_upload:
                release_confirmed(client, journal, args.activity_id)
                print("Claim released. Inspect orphan media before retrying the activity.")
                return 0
            if entry.get("workout_id") and entry["workout_id"] != args.workout_id:
                raise ImportProblem("Workout ID conflicts with the journal")
            # Registration checks actual native ownership, even for public workouts.
            client.request(
                "GET",
                PREFIX + "/workouts/" + quote(args.workout_id, safe="") + "/identities",
            )
            entry["workout_id"] = args.workout_id
            journal.save(args.activity_id, entry)
            bind(client, journal, args.activity_id, entry)
            print("Identity bound to the supplied workout. Future imports skip it.")
            return 0
        assert archive is not None
        if not client:
            print("Offline dryrun: identities and target sport availability are unverified.")
            print("Manual date preview timezone: " + timezone)
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
        )
        print("Summary: " + json.dumps(dict(sorted(counts.items()))))
        return 1 if counts["blocked"] else 0
    finally:
        for resource in (archive, journal, client, lock):
            if resource:
                resource.close()


def main() -> None:
    args = parser().parse_args()
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
    raise SystemExit(code)
