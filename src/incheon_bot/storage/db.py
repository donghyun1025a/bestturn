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

-- 항공편 메모 (book 등록 여부와 무관하게 유지)
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    flight_no   TEXT    NOT NULL,
    search_date TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    author      TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_flight ON notes (chat_id, flight_no, search_date);

-- 사용자 지정 사전 알림 (출발/도착 N분 전)
CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    flight_no      TEXT    NOT NULL,
    search_date    TEXT    NOT NULL,
    minutes_before INTEGER NOT NULL,
    text           TEXT    NOT NULL,
    sent           INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_flight ON reminders (chat_id, flight_no, search_date, sent);
"""

# 기존 DB 에 나중에 추가된 컬럼 (없을 때만 붙입니다)
MIGRATIONS = {
    "bookings": {"sent_reminders": "TEXT NOT NULL DEFAULT '[]'"},
}


@dataclass(frozen=True)
class Note:
    id: int
    chat_id: int
    flight_no: str
    search_date: str
    text: str
    author: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class CustomReminder:
    id: int
    chat_id: int
    flight_no: str
    search_date: str
    minutes_before: int
    text: str
    sent: bool


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
    sent_reminders: list[str]

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


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _row_to_booking(row: sqlite3.Row) -> Booking:
    try:
        snapshot = json.loads(row["snapshot"] or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    keys = row.keys()
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
        sent_reminders=_json_list(row["sent_reminders"] if "sent_reminders" in keys else "[]"),
    )


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        chat_id=row["chat_id"],
        flight_no=row["flight_no"],
        search_date=row["search_date"],
        text=row["text"],
        author=row["author"],
        created_at=_to_dt(row["created_at"]),
    )


def _row_to_reminder(row: sqlite3.Row) -> CustomReminder:
    return CustomReminder(
        id=row["id"],
        chat_id=row["chat_id"],
        flight_no=row["flight_no"],
        search_date=row["search_date"],
        minutes_before=row["minutes_before"],
        text=row["text"],
        sent=bool(row["sent"]),
    )


class BookingStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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
        sent_reminders: list[str] | None = None,
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
                           search_date=?, fid=?, label=?, sent_reminders=?
                       WHERE id=?""",
                    (
                        json.dumps(snapshot, ensure_ascii=False),
                        next_check_at.isoformat(timespec="seconds"),
                        now,
                        search_date,
                        fid,
                        label,
                        json.dumps(sent_reminders or _json_list(existing["sent_reminders"]), ensure_ascii=False),
                        existing["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM bookings WHERE id=?", (existing["id"],)).fetchone()
                return _row_to_booking(row), not bool(existing["active"])
            cursor = conn.execute(
                """INSERT INTO bookings
                   (chat_id, user_id, flight_key, fid, flight_no, direction, search_date,
                    label, snapshot, active, next_check_at, created_at, updated_at, sent_reminders)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
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
                    json.dumps(sent_reminders or [], ensure_ascii=False),
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

    def mark_reminders_sent(self, booking_id: int, keys: list[str]) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT sent_reminders FROM bookings WHERE id=?", (booking_id,)).fetchone()
            merged = sorted(set(_json_list(row["sent_reminders"] if row else "[]")) | set(keys))
            conn.execute(
                "UPDATE bookings SET sent_reminders=?, updated_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), booking_id),
            )

    # ------------------------------------------------------------------- 메모
    def add_note(self, *, chat_id: int, flight_no: str, search_date: str, text: str, author: str | None) -> Note:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO notes (chat_id, flight_no, search_date, text, author, created_at) VALUES (?,?,?,?,?,?)",
                (chat_id, flight_no.upper(), search_date, text, author, now),
            )
            row = conn.execute("SELECT * FROM notes WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _row_to_note(row)

    def notes(self, chat_id: int, flight_no: str, search_date: str) -> list[Note]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM notes WHERE chat_id=? AND flight_no=? AND search_date=? ORDER BY id",
                (chat_id, flight_no.upper(), search_date),
            ).fetchall()
        return [_row_to_note(r) for r in rows]

    def delete_note(self, chat_id: int, flight_no: str, search_date: str, index: int) -> Note | None:
        """1부터 시작하는 표시 순번으로 삭제."""
        items = self.notes(chat_id, flight_no, search_date)
        if not 1 <= index <= len(items):
            return None
        target = items[index - 1]
        with self._conn() as conn:
            conn.execute("DELETE FROM notes WHERE id=?", (target.id,))
        return target

    def clear_notes(self, chat_id: int, flight_no: str, search_date: str) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM notes WHERE chat_id=? AND flight_no=? AND search_date=?",
                (chat_id, flight_no.upper(), search_date),
            )
            return cursor.rowcount

    # -------------------------------------------------------- 사용자 지정 사전 알림
    def add_reminder(
        self, *, chat_id: int, flight_no: str, search_date: str, minutes_before: int, text: str
    ) -> CustomReminder:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO reminders (chat_id, flight_no, search_date, minutes_before, text, sent, created_at)
                   VALUES (?,?,?,?,?,0,?)""",
                (chat_id, flight_no.upper(), search_date, minutes_before, text, now),
            )
            row = conn.execute("SELECT * FROM reminders WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _row_to_reminder(row)

    def reminders(
        self, chat_id: int, flight_no: str, search_date: str, *, pending_only: bool = False
    ) -> list[CustomReminder]:
        clause = " AND sent=0" if pending_only else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM reminders
                    WHERE chat_id=? AND flight_no=? AND search_date=?{clause}
                    ORDER BY minutes_before DESC""",
                (chat_id, flight_no.upper(), search_date),
            ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))

    def clear_reminders(self, chat_id: int, flight_no: str, search_date: str) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM reminders WHERE chat_id=? AND flight_no=? AND search_date=?",
                (chat_id, flight_no.upper(), search_date),
            )
            return cursor.rowcount
