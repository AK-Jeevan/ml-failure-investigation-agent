"""Small SQLite persistence layer for investigation state and trajectory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .models import Investigation, InvestigationStatus, utc_now


class InvestigationStore:
    def __init__(self, database_path: str | Path = "data/investigations.sqlite3") -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS investigations (
                    investigation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            existing_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(investigations)").fetchall()
            }
            for column in ("owner_id", "heartbeat_at"):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE investigations ADD COLUMN {column} TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, investigation: Investigation, owner_id: str | None = None) -> None:
        """Persist investigation state; every write also refreshes the liveness heartbeat."""
        payload = investigation.to_dict()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO investigations(investigation_id, status, updated_at, payload, owner_id, heartbeat_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(investigation_id) DO UPDATE SET
                     status=excluded.status, updated_at=excluded.updated_at,
                     payload=excluded.payload,
                     owner_id=COALESCE(excluded.owner_id, investigations.owner_id),
                     heartbeat_at=excluded.heartbeat_at""",
                (
                    investigation.incident.incident_id,
                    investigation.status.value,
                    investigation.trajectory[-1].created_at if investigation.trajectory else investigation.incident.observed_at,
                    json.dumps(payload, ensure_ascii=False),
                    owner_id,
                    utc_now(),
                ),
            )

    def touch_heartbeat(self, investigation_ids: Iterable[str], owner_id: str) -> int:
        """Refresh heartbeat timestamps for running investigations owned by one API process."""
        identifiers = list(dict.fromkeys(investigation_ids))
        if not identifiers:
            return 0
        placeholders = ", ".join("?" for _ in identifiers)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""UPDATE investigations SET heartbeat_at = ?
                    WHERE owner_id = ? AND status = ? AND investigation_id IN ({placeholders})""",
                (utc_now(), owner_id, InvestigationStatus.RUNNING.value, *identifiers),
            )
            return int(cursor.rowcount)

    def stale_running(self, stale_before: str, owner_id: str | None = None) -> list[dict]:
        """Return running investigations whose heartbeat is missing or older than a cutoff.

        Rows owned by ``owner_id`` are excluded so a live API process never reconciles its own work.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT investigation_id, owner_id, heartbeat_at FROM investigations
                   WHERE status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
                (InvestigationStatus.RUNNING.value, stale_before),
            ).fetchall()
        return [dict(row) for row in rows if owner_id is None or row["owner_id"] != owner_id]

    def load_payload(self, investigation_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_status(self, investigation_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT investigation_id, status, updated_at FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM investigations GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def ping(self) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def save_payload(self, investigation_id: str, payload: dict) -> None:
        status = payload.get("status")
        status_value = status.value if isinstance(status, InvestigationStatus) else str(status or "unknown")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE investigations SET status = ?, updated_at = ?, payload = ? WHERE investigation_id = ?",
                (status_value, utc_now(), json.dumps(payload, ensure_ascii=False), investigation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Investigation {investigation_id!r} was not found")
