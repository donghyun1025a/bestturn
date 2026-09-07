from datetime import date, datetime

import pytest

from conftest import arrival_row, departure_row
from incheon_bot.api.models import Congestion, Flight, normalize_flight_no, parse_dt
from incheon_bot.domain.query import QueryError, looks_like_flight_query, parse_query


def test_flight_parses_both_key_casings():
    """응답 문서(scheduleDatetime)와 샘플 payload(scheduleDateTime) 표기가 다릅니다."""
    ob = Flight.from_raw(departure_row(), "OB")
    ib = Flight.from_raw(arrival_row(), "IB")
    assert ob.schedule_dt == datetime(2026, 9, 7, 18, 0)
    assert ob.estimated_dt == datetime(2026, 9, 7, 18, 15)
    assert ib.schedule_dt == datetime(2026, 9, 7, 12, 15)
    assert ib.aircraft_subtype == "32N" and ob.aircraft_subtype == "333"


def test_empty_tokens_become_none():
    flight = Flight.from_raw(departure_row(gateNumber="-", remark="없음", chkinRange=""), "OB")
    assert flight.gate_number is None
    assert flight.remark is None
    assert flight.checkin_range is None


def test_delay_and_direction_flags():
    ob = Flight.from_raw(departure_row(), "OB")
    assert ob.is_delayed is True
    assert ob.is_inbound is False
    assert ob.carrier_code == "WE"
    assert Flight.from_raw(arrival_row(), "IB").is_inbound is True


def test_key_falls_back_without_fid():
    flight = Flight.from_raw(departure_row(fid=""), "OB")
    assert flight.key == "OB:WE501:202609071800"


@pytest.mark.parametrize(
    "raw,expected",
    [("we 501", "WE501"), ("KE0086", "KE86"), ("5Y8232", "5Y8232"), ("", "")],
)
def test_normalize_flight_no(raw, expected):
    assert normalize_flight_no(raw) == expected


def test_parse_dt_handles_2400():
    assert parse_dt("202609072400") == datetime(2026, 9, 7, 23, 59)
    assert parse_dt("bad") is None


def test_congestion_60_plus():
    item = Congestion.from_raw({"gateId": "DG6_E", "waitTime": "60+", "waitLength": "310"})
    assert item.wait_time_min == 60
    assert item.wait_time_raw == "60+"
    assert item.is_open is True


def test_congestion_closed_gate():
    item = Congestion.from_raw({"gateId": "DG2_W", "waitTime": "-", "operatingTime": "05:00~22:00"})
    assert item.is_open is False


@pytest.mark.parametrize(
    "text,flight_no,search_date",
    [
        ("WE501", "WE501", "20260907"),
        ("WE 501 0908", "WE501", "20260908"),
        ("20260910 KE86", "KE86", "20260910"),
        ("ke0086 내일", "KE86", "20260908"),
        ("oz102 2026-09-10", "OZ102", "20260910"),
        ("5Y8232 모레", "5Y8232", "20260909"),
    ],
)
def test_parse_query(text, flight_no, search_date):
    query = parse_query(text, today=date(2026, 9, 7))
    assert (query.flight_no, query.search_date) == (flight_no, search_date)


@pytest.mark.parametrize("text", ["", "안녕하세요", "WE501 20270101", "WE501 20260101"])
def test_parse_query_errors(text):
    with pytest.raises(QueryError):
        parse_query(text, today=date(2026, 9, 7))


def test_looks_like_flight_query():
    assert looks_like_flight_query("we501") is True
    assert looks_like_flight_query("/book WE501") is False
    assert looks_like_flight_query("점심 뭐먹지") is False
