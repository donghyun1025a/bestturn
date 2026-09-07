from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import CONFIG_DIR, arrival_row, congestion_row, departure_row, envelope
from incheon_bot.api.client import IncheonAirportClient
from incheon_bot.api.models import Flight
from incheon_bot.bot import formatting as fmt
from incheon_bot.domain.reminders import ReminderConfig, custom_reminder_due
from incheon_bot.service import BriefingService
from incheon_bot.storage.db import BookingStore
from incheon_bot.watcher import watch_tick

OB = Flight.from_raw(departure_row(scheduleDateTime="202609071800", estimatedDateTime="202609071800"), "OB")
IB = Flight.from_raw(arrival_row(scheduleDatetime="202609071215", estimatedDatetime="202609071215"), "IB")


@pytest.fixture(scope="module")
def rules() -> ReminderConfig:
    return ReminderConfig(CONFIG_DIR / "reminders.yml")


# ----------------------------------------------------------------- 사전 알림 규칙
def test_reminder_fires_only_inside_its_window(rules):
    assert rules.evaluate(OB, [], now=datetime(2026, 9, 7, 13, 59))[0] == []
    due, _ = rules.evaluate(OB, [], now=datetime(2026, 9, 7, 14, 0))
    assert [d.key for d in due] == ["checkin"]          # T-4h
    due, _ = rules.evaluate(OB, [], now=datetime(2026, 9, 7, 17, 25))
    assert [d.key for d in due] == ["boarding"]         # T-40m (T-1h 은 유예 초과)


def test_missed_reminders_are_skipped_not_dumped(rules):
    """유예시간(20분)을 넘긴 알림은 발송하지 않고 완료 처리한다."""
    due, expired = rules.evaluate(OB, [], now=datetime(2026, 9, 7, 16, 1))
    assert [d.key for d in due] == ["security"]
    assert set(expired) == {"checkin", "meet"}


def test_already_sent_reminders_do_not_repeat(rules):
    due, _ = rules.evaluate(OB, ["checkin"], now=datetime(2026, 9, 7, 14, 5))
    assert due == []


def test_delay_shifts_pending_reminders(rules):
    """지연되면 아직 안 보낸 알림은 변경시각 기준으로 밀린다."""
    delayed = Flight.from_raw(
        departure_row(scheduleDateTime="202609071800", estimatedDateTime="202609071900"), "OB"
    )
    # 원래 시각(18:00) 기준이면 17:05 는 T-55m 이지만, 변경 시각(19:00) 기준으로는 T-2h 구간
    due, _ = rules.evaluate(delayed, [], now=datetime(2026, 9, 7, 17, 5))
    assert [d.key for d in due] == ["security"]
    # 원래 시각이라면 이미 출발했을 18:00 에 T-1h 알림이 나간다
    due, _ = rules.evaluate(delayed, ["checkin", "meet", "security"], now=datetime(2026, 9, 7, 18, 0))
    assert [d.key for d in due] == ["gate"]


def test_inbound_rules_include_post_landing(rules):
    due, _ = rules.evaluate(IB, [], now=datetime(2026, 9, 7, 12, 35))
    assert [d.key for d in due] == ["arrival_hall"]      # 도착 +20분
    assert rules.evaluate(IB, [], now=datetime(2026, 9, 7, 11, 50))[0][0].key == "landing"


def test_booking_at_the_last_minute_does_not_backfill(rules):
    """출발 직전에 등록해도 지난 알림이 한꺼번에 오지 않는다."""
    already = rules.initial_sent_keys(OB, now=datetime(2026, 9, 7, 17, 30))
    assert set(already) == {"checkin", "meet", "security", "gate"}
    # 아직 유효한 T-40m 알림만 나가고, 지나간 4건이 한꺼번에 오지는 않는다
    due, _ = rules.evaluate(OB, already, now=datetime(2026, 9, 7, 17, 30))
    assert [d.key for d in due] == ["boarding"]
    # 출발 직후 등록이면 아무 알림도 나가지 않는다
    assert rules.evaluate(OB, rules.initial_sent_keys(OB, now=datetime(2026, 9, 7, 18, 30)),
                          now=datetime(2026, 9, 7, 18, 30))[0] == []


def test_custom_reminder_window():
    assert custom_reminder_due(90, OB, now=datetime(2026, 9, 7, 16, 30)) is True
    assert custom_reminder_due(90, OB, now=datetime(2026, 9, 7, 16, 29)) is False
    assert custom_reminder_due(90, OB, now=datetime(2026, 9, 7, 17, 0)) is False   # 유예 초과


# ------------------------------------------------------------------------ 메모
def test_notes_crud(tmp_path):
    store = BookingStore(tmp_path / "b.db")
    store.add_note(chat_id=1, flight_no="WE501", search_date="20260907", text="VIP 3명", author="김의전")
    store.add_note(chat_id=1, flight_no="WE501", search_date="20260907", text="휠체어 1대", author="김의전")

    notes = store.notes(1, "WE501", "20260907")
    assert [n.text for n in notes] == ["VIP 3명", "휠체어 1대"]
    assert store.notes(1, "WE501", "20260908") == []       # 날짜별로 분리
    assert store.notes(2, "WE501", "20260907") == []       # 대화방별로 분리

    removed = store.delete_note(1, "WE501", "20260907", 1)
    assert removed.text == "VIP 3명"
    assert [n.text for n in store.notes(1, "WE501", "20260907")] == ["휠체어 1대"]
    assert store.delete_note(1, "WE501", "20260907", 9) is None
    assert store.clear_notes(1, "WE501", "20260907") == 1


def test_notes_survive_booking_close(tmp_path):
    store = BookingStore(tmp_path / "b.db")
    booking, _ = store.upsert(
        chat_id=1, user_id=1, flight_key="k", fid="F", flight_no="WE501", direction="OB",
        search_date="20260907", label="OB", snapshot={}, next_check_at=datetime.now(),
    )
    store.add_note(chat_id=1, flight_no="WE501", search_date="20260907", text="게이트 앞 미팅", author=None)
    store.close(booking.id)
    assert len(store.notes(1, "WE501", "20260907")) == 1


def test_note_rendering_escapes_html():
    note = SimpleNamespace(text="<b>VIP</b> & 수행 2명", author="김의전", created_at=datetime(2026, 9, 7, 9, 0))
    rendered = fmt.render_notes([note])
    assert "&lt;b&gt;VIP&lt;/b&gt; &amp; 수행 2명" in rendered
    assert fmt.render_notes([]) == ""


def test_reminder_store_roundtrip(tmp_path):
    store = BookingStore(tmp_path / "b.db")
    store.add_reminder(chat_id=1, flight_no="WE501", search_date="20260907", minutes_before=90, text="배차 확인")
    pending = store.reminders(1, "WE501", "20260907", pending_only=True)
    assert len(pending) == 1 and pending[0].minutes_before == 90

    store.mark_reminder_sent(pending[0].id)
    assert store.reminders(1, "WE501", "20260907", pending_only=True) == []
    assert store.reminders(1, "WE501", "20260907")[0].sent is True
    assert store.clear_reminders(1, "WE501", "20260907") == 1


def test_sent_reminders_column_is_added_to_existing_db(tmp_path):
    """구버전 DB 를 열어도 sent_reminders 컬럼이 자동으로 추가된다."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE bookings (
             id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER,
             flight_key TEXT NOT NULL, fid TEXT, flight_no TEXT NOT NULL, direction TEXT NOT NULL,
             search_date TEXT NOT NULL, label TEXT, snapshot TEXT NOT NULL DEFAULT '{}',
             active INTEGER NOT NULL DEFAULT 1, next_check_at TEXT, created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL, closed_at TEXT, UNIQUE (chat_id, flight_key));"""
    )
    conn.execute(
        "INSERT INTO bookings (chat_id, flight_key, flight_no, direction, search_date, created_at, updated_at)"
        " VALUES (1,'k','WE501','OB','20260907','x','x')"
    )
    conn.commit()
    conn.close()

    store = BookingStore(path)
    assert store.list_active(1)[0].sent_reminders == []
    store.mark_reminders_sent(store.list_active(1)[0].id, ["checkin"])
    assert store.list_active(1)[0].sent_reminders == ["checkin"]
