"""book 예약(감시) 상태 저장소 (SQLite)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    user_id        INTEGER,
    flight_key     TEXT    NOT NULL,
    fid            TEXT,
    flight_no      TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    search_date    TEXT    NOT NULL,
    label          TEXT,
    snapshot       TEXT    NOT NULL DEFAULT '{}',
    active         INTEGER NOT NULL DEFAULT 1,
    next_check_at  TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    closed_at      TEXT,
    UNIQUE (chat_id, flight_key)
);
CREATE INDEX IF NOT EXISTS idx_bookings_active ON bookings (active, next_check_at);
"""


@dataclass
class Booking:
    id: int
    chat_id: int
    user_id: int | None
    flight_key: str
    fid: str | None
    flight_no: str
    direction: str
    search_date: str
    label: str | None
    snapshot: dict[str, str | None]
    active: bool
    next_check_at: datetime | None

    @property
    def is_inbound(self) -> bool:
        return self.direction == "IB"


def _to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_booking(row: sqlite3.Row) -> Booking:
    try:
        snapshot = json.loads(row["snapshot"] or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    return Booking(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        flight_key=row["flight_key"],
        fid=row["fid"],
        flight_no=row["flight_no"],
        direction=row["direction"],
        search_date=row["search_date"],
        label=row["label"],
        snapshot=snapshot,
        active=bool(row["active"]),
        next_check_at=_to_dt(row["next_check_at"]),
    )


class BookingStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        flight_key: str,
        fid: str | None,
        flight_no: str,
        direction: str,
        search_date: str,
        label: str | None,
        snapshot: dict[str, str | None],
        next_check_at: datetime,
    ) -> tuple[Booking, bool]:
        """(booking, created) 반환. 이미 감시 중이면 재활성화."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM bookings WHERE chat_id=? AND flight_key=?", (chat_id, flight_key)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE bookings
                       SET active=1, snapshot=?, next_check_at=?, updated_at=?, closed_at=NULL,
                           search_date=?, fid=?, label=?
                       WHERE id=?""",
                    (
                        json.dumps(snapshot, ensure_ascii=False),
                        next_check_at.isoformat(timespec="seconds"),
                        now,
                        search_date,
                        fid,
                        label,
                        existing["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM bookings WHERE id=?", (existing["id"],)).fetchone()
                return _row_to_booking(row), not bool(existing["active"])
            cursor = conn.execute(
                """INSERT INTO bookings
                   (chat_id, user_id, flight_key, fid, flight_no, direction, search_date,
                    label, snapshot, active, next_check_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (
                    chat_id,
                    user_id,
                    flight_key,
                    fid,
                    flight_no,
                    direction,
                    search_date,
                    label,
                    json.dumps(snapshot, ensure_ascii=False),
                    next_check_at.isoformat(timespec="seconds"),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (cursor.lastrowid,)).fetchone()
            return _row_to_booking(row), True

    def due(self, *, now: datetime | None = None, limit: int = 50) -> list[Booking]:
        now = now or datetime.now()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM bookings
                   WHERE active=1 AND (next_check_at IS NULL OR next_check_at <= ?)
                   ORDER BY next_check_at IS NULL DESC, next_check_at ASC LIMIT ?""",
                (now.isoformat(timespec="seconds"), limit),
            ).fetchall()
        return [_row_to_booking(r) for r in rows]

    def list_active(self, chat_id: int) -> list[Booking]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE chat_id=? AND active=1 ORDER BY search_date, flight_no",
                (chat_id,),
            ).fetchall()
        return [_row_to_booking(r) for r in rows]

    def find(self, chat_id: int, flight_no: str) -> list[Booking]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE chat_id=? AND active=1 AND flight_no=?",
                (chat_id, flight_no.upper()),
            ).fetchall()
        return [_row_to_booking(r) for r in rows]

    def update_snapshot(self, booking_id: int, snapshot: dict[str, str | None], next_check_at: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bookings SET snapshot=?, next_check_at=?, updated_at=? WHERE id=?",
                (
                    json.dumps(snapshot, ensure_ascii=False),
                    next_check_at.isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                    booking_id,
                ),
            )

    def reschedule(self, booking_id: int, next_check_at: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bookings SET next_check_at=? WHERE id=?",
                (next_check_at.isoformat(timespec="seconds"), booking_id),
            )

    def close(self, booking_id: int) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "UPDATE bookings SET active=0, closed_at=?, updated_at=? WHERE id=?",
                (now, now, booking_id),
            )

    def close_all(self, chat_id: int) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE bookings SET active=0, closed_at=?, updated_at=? WHERE chat_id=? AND active=1",
                (now, now, chat_id),
            )
            return cursor.rowcount
