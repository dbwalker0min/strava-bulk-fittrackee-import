"""Durable, private per-server/account journal. Never authorize uncertain upload retries."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from .client import private_dir
from .errors import ImportProblem
from .models import JournalEntry, journal_entry


class Journal:
    def __init__(self, path: Path, namespace: str) -> None:
        private_dir(path.parent)
        self.db = sqlite3.connect(path, timeout=5)
        if os.name != "nt":
            path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS entries "
            "(namespace TEXT, external_id TEXT, data TEXT NOT NULL, "
            "PRIMARY KEY(namespace, external_id))"
        )
        self.namespace = namespace

    def close(self) -> None:
        self.db.close()

    def get(self, external_id: str) -> JournalEntry | None:
        row = self.db.execute(
            "SELECT data FROM entries WHERE namespace=? AND external_id=?",
            (self.namespace, external_id),
        ).fetchone()
        return journal_entry(json.loads(row[0])) if row else None

    def save(self, external_id: str, data: JournalEntry) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO entries VALUES (?, ?, ?) ON CONFLICT(namespace, external_id) "
                "DO UPDATE SET data=excluded.data",
                (self.namespace, external_id, json.dumps(data)),
            )


class RunLock:
    """OS advisory lock, released even if the process crashes; shared by login/import/recovery."""

    def __init__(self, directory: Path) -> None:
        private_dir(directory)
        self.handle = (directory / "run.lock").open("a+b")
        self.handle.write(b"\0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ImportProblem("Another importer/login is using this state directory") from exc

    def close(self) -> None:
        self.handle.close()
