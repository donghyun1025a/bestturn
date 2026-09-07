from datetime import datetime

import pytest

from conftest import arrival_row, congestion_row, departure_row
from incheon_bot.api.models import Congestion, Flight
from incheon_bot.domain.status import (
    adaptive_interval_minutes,
    diff_flights,
    should_stop_watching,
    status_emoji,
)


@pytest.mark.parametrize(
    "minutes,level",
    [(0, "원활"), (19, "원활"), (20, "보통"), (39, "보통"), (40, "혼잡"), (59, "혼잡"), (60, "매우혼잡"), (None, "정보없음")],
)
def test_congestion_levels_match_spec(routing, minutes, level):
    """제공기관 별첨 기준: 20분 미만 원활 / 20~40 보통 / 40~60 혼잡 / 60분 이상 매우혼잡."""
    assert routing.level_for(minutes)[0] == level


def test_route_prefers_near_and_uncongested(routing):
    congestion = [
        Congestion.from_raw(congestion_row("DG1_E", 45)),
        Congestion.from_raw(congestion_row("DG1_W", 8)),
        Congestion.from_raw(congestion_row("DG6_E", 3)),
    ]
    options, origin = routing.route_options("P01", congestion, gate_number="11", checkin_range="A-B")
    assert origin == "체크인 카운터 A-B"
    best = next(o for o in options if o.has_data)
    # 가까우면서 대기 짧은 1번 출국장 서편이 선택되어야 한다 (6번은 멀고, 1번 동편은 혼잡)
    assert best.gate.gate_id == "DG1_W"
    ids = [o.gate.gate_id for o in options if o.has_data]
    assert ids.index("DG1_W") < ids.index("DG6_E") < ids.index("DG1_E")


def test_route_falls_back_to_gate_position_without_counter(routing):
    congestion = [Congestion.from_raw(congestion_row(g, 10)) for g in ("DG1_W", "DG6_W")]
    options, origin = routing.route_options("P01", congestion, gate_number="45", checkin_range=None)
    assert origin == "탑승구 45"
    assert next(o for o in options if o.has_data).gate.gate_id == "DG6_W"


def test_closed_gate_is_not_recommended(routing):
    congestion = [
        Congestion.from_raw(congestion_row("DG1_W", "-")),
        Congestion.from_raw(congestion_row("DG2_E", 12)),
    ]
    options, _ = routing.route_options("P01", congestion, gate_number="11")
    best = next(o for o in options if o.has_data)
    assert best.gate.gate_id == "DG2_E"


def test_concourse_uses_t1_congestion(routing):
    terminal, api = routing.congestion_terminal("P02")
    assert terminal.code == "P01" and api == "T1"
    assert "셔틀트레인" in routing.terminal("P02").transfer_note


def test_t2_uses_its_own_api(routing):
    terminal, api = routing.congestion_terminal("P03")
    assert terminal.code == "P03" and api == "T2"
    assert set(terminal.departure_gates) >= {"DG1_A", "DG2_D"}


def test_immigration_prefers_exit_number(routing):
    """입국심사대는 API 의 출구(exitNumber) 로 확정 안내한다."""
    info = routing.immigration_for("P01", gate_number="42", exit_number="B")
    assert info.is_confirmed and info.exit_code == "B"
    assert "동편" in info.hall                    # 출구 B 는 동편 — 탑승구(42, 서편)보다 출구가 우선
    assert info.render() == "출구 B · 동편 입국심사장 (2F)"

    t2 = routing.immigration_for("P03", exit_number="출구 A")
    assert t2.exit_code == "A" and "T2 동편" in t2.hall


def test_immigration_falls_back_to_gate_before_exit_is_assigned(routing):
    info = routing.immigration_for("P01", gate_number="8")
    assert info.source == "gate" and info.exit_code is None
    assert "동편" in info.hall and "추정" in info.render()
    assert "서편" in routing.immigration_for("P01", gate_number="42").hall
    assert "셔틀트레인" in routing.immigration_for("P02", gate_number="118").hall


def test_unknown_exit_code_is_still_reported(routing):
    """설정에 없는 출구라도 출구 자체는 확정 정보이므로 그대로 안내한다."""
    info = routing.immigration_for("P01", exit_number="Z")
    assert info.is_confirmed and info.exit_code == "Z"


def test_status_emoji_and_terminal_status():
    assert status_emoji("탑승중") == "🛫"
    assert status_emoji("결항") == "❌"
    assert status_emoji(None) == "•"


def test_diff_detects_new_and_changed_values():
    changes = diff_flights({"탑승구": "11", "현황": None}, {"탑승구": "26", "현황": "탑승중"})
    assert {c.field for c in changes} == {"탑승구", "현황"}
    assert "<b>26</b>" in changes[0].render()
    assert diff_flights({"탑승구": "11"}, {"탑승구": "11"}) == []


def test_adaptive_interval_tightens_near_departure():
    flight = Flight.from_raw(departure_row(scheduleDateTime="202609071800", estimatedDateTime=""), "OB")
    assert adaptive_interval_minutes(flight, now=datetime(2026, 9, 7, 6, 0)) == 20
    assert adaptive_interval_minutes(flight, now=datetime(2026, 9, 7, 14, 0)) == 10
    assert adaptive_interval_minutes(flight, now=datetime(2026, 9, 7, 16, 0)) == 5
    assert adaptive_interval_minutes(flight, now=datetime(2026, 9, 7, 17, 30)) == 2


def test_stop_watching_rules():
    cancelled = Flight.from_raw(departure_row(remark="결항"), "OB")
    assert should_stop_watching(cancelled, now=datetime(2026, 9, 7, 8, 0)) is True

    departed = Flight.from_raw(departure_row(remark="출발"), "OB")
    assert should_stop_watching(departed, now=datetime(2026, 9, 7, 18, 30)) is False   # 유예 90분 이내
    assert should_stop_watching(departed, now=datetime(2026, 9, 7, 20, 30)) is True

    arrived = Flight.from_raw(arrival_row(remark="도착"), "IB")
    assert should_stop_watching(arrived, now=datetime(2026, 9, 7, 15, 0)) is True
