"""도착 예측용 SQLite 저장소."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .protocol import ETA_PRIORITY, EPOCH_FIELDS, TEXT_FIELDS, Update, ident_airline

# flights 에 그대로 들어가는 컬럼. protocol 의 필드명을 컬럼명으로 씁니다.
_COLUMNS = (*EPOCH_FIELDS, *(name for name in TEXT_FIELDS if name != "ident"))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS flights (
    flight_id   TEXT PRIMARY KEY,
    ident       TEXT NOT NULL,
    airline     TEXT,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    eta         INTEGER,
    eta_source  TEXT,
    last_pitr   INTEGER,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    {", ".join(f"{name} {'INTEGER' if name in EPOCH_FIELDS else 'TEXT'}" for name in _COLUMNS)}
);
CREATE INDEX IF NOT EXISTS idx_flights_ident ON flights (ident, eta);
CREATE INDEX IF NOT EXISTS idx_flights_dest ON flights (dest, eta);

-- 도착 예정시각이 바뀔 때마다 한 줄. 예측 모델 학습용 이력입니다.
CREATE TABLE IF NOT EXISTS eta_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_id   TEXT    NOT NULL,
    ident       TEXT    NOT NULL,
    msg_type    TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    eta         INTEGER NOT NULL,
    pitr        INTEGER,
    FOREIGN KEY (flight_id) REFERENCES flights (flight_id)
);
CREATE INDEX IF NOT EXISTS idx_eta_history_flight ON eta_history (flight_id, id);

CREATE TABLE IF NOT EXISTS stream_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- UI 에서 입력한 설정. 자격증명이 들어 있어 DB 파일 권한을 0600 으로 제한합니다.
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _best_eta(row: dict) -> tuple[str, int] | None:
    for name in ETA_PRIORITY:
        value = row.get(name)
        if isinstance(value, int):
            return name, value
    return None


class EtaStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        # Firehose API Key 가 app_config 에 들어가므로 소유자만 읽을 수 있게 합니다.
        self.path.chmod(0o600)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def apply(self, update: Update) -> bool:
        """변경분을 반영하고, 도착 예정시각이 바뀌었으면 True 를 돌려줍니다."""
        now = update.pitr or 0
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM flights WHERE flight_id=?", (update.flight_id,)
            ).fetchone()
            merged = dict(existing) if existing else {}
            before = merged.get("eta")
            merged.update(update.fields)

            best = _best_eta(merged)
            merged["eta"], merged["eta_source"] = (best[1], best[0]) if best else (None, None)

            values = {name: merged.get(name) for name in _COLUMNS}
            values.update(
                ident=update.ident or merged.get("ident") or "",
                airline=ident_airline(update.ident) or merged.get("airline"),
                cancelled=int(merged.get("cancelled") or 0),
                eta=merged["eta"],
                eta_source=merged["eta_source"],
                last_pitr=update.pitr,
                last_seen=now,
            )
            if existing:
                assignments = ", ".join(f"{name}=:{name}" for name in values)
                conn.execute(
                    f"UPDATE flights SET {assignments} WHERE flight_id=:flight_id",
                    {**values, "flight_id": update.flight_id},
                )
            else:
                values["flight_id"] = update.flight_id
                values["first_seen"] = now
                names = ", ".join(values)
                conn.execute(
                    f"INSERT INTO flights ({names}) VALUES ({', '.join(':' + n for n in values)})",
                    values,
                )

            changed = best is not None and best[1] != before
            if changed:
                conn.execute(
                    "INSERT INTO eta_history (flight_id, ident, msg_type, source, eta, pitr)"
                    " VALUES (?,?,?,?,?,?)",
                    (update.flight_id, values["ident"], update.msg_type, best[0], best[1], update.pitr),
                )
            return changed

    def flight(self, flight_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
        return dict(row) if row else None

    def by_ident(self, ident: str, *, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM flights WHERE ident=? ORDER BY eta IS NULL, eta DESC LIMIT ?",
                (ident.strip().upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def arrivals(self, dest: str, *, since: int, until: int, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM flights
                   WHERE dest=? AND cancelled=0 AND eta BETWEEN ? AND ?
                   ORDER BY eta LIMIT ?""",
                (dest.strip().upper(), since, until, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def history(self, flight_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT msg_type, source, eta, pitr FROM eta_history WHERE flight_id=? ORDER BY id",
                (flight_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def config(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM app_config").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def save_config(self, values: dict[str, str]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO app_config (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(values.items()),
            )

    def get_pitr(self) -> int | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM stream_state WHERE key='pitr'").fetchone()
        return int(row["value"]) if row else None

    def set_pitr(self, pitr: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO stream_state (key, value) VALUES ('pitr', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(pitr),),
            )
