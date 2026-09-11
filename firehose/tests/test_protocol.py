"""Firehose 실제 메시지 모양(FlightAware 공개 픽스처 기준)에 대한 회귀 테스트."""
from __future__ import annotations

from eta_ingest.protocol import accepted_prefixes, ident_airline, init_command, normalize

# flifo 는 긴 이름(estimated_on), flightplan/position 은 축약형(eta)을 씁니다.
FLIFO = {
    "pitr": "1647160200", "type": "flifo", "ident": "THA657", "dest": "RKSI",
    "actual_off": "1647159545", "estimated_on": "1647170100", "ete": "10555",
    "id": "THA657-1646986800-schedule-0083", "orig": "VTBS", "reg": "HSTBA",
    "scheduled_off": "1647159300", "scheduled_out": "1647159300", "status": "A",
}
FLIGHTPLAN = {
    "pitr": "1589549426", "type": "flightplan", "ident": "AAL281", "dest": "RKSI",
    "alt": "6000", "edt": "1589549400", "eta": "1589552338", "fdt": "1589548500",
    "id": "AAL281-1589545563-3-1-67", "orig": "KDFW", "predicted_on": "1589552000",
    "reg": "N787AL", "speed": "112", "status": "F",
}
ARRIVAL = {
    "pitr": "1589549426", "type": "arrival", "ident": "AAL281", "dest": "RKSI",
    "id": "AAL281-1589545563-3-1-67", "orig": "KDFW", "aat": "1589549221",
    "timeType": "estimated", "synthetic": "1",
}
ONBLOCK = {
    "pitr": "1589549500", "type": "onblock", "ident": "AAL281", "clock": "1589549800",
    "orig": "KDFW", "dest": "RKSI", "id": "AAL281-1589545563-3-1-67",
}


def test_init_command_quotes_multi_value_args():
    command = init_command(username="u", password="k", airport="ICN")
    assert command.startswith("live username u password k useragent bestturn-eta")
    assert 'events "flifo arrival departure cancellation position"' in command
    assert 'airport_filter "RKSI"' in command  # ICN 은 ICAO 로 바꿔 보냅니다
    assert command.endswith("\n")


def test_init_command_resumes_from_pitr():
    assert init_command(username="u", password="k", time_mode="pitr 123").startswith("pitr 123 ")


def test_flifo_long_names_and_flightplan_short_names_land_in_one_field():
    assert normalize(FLIFO).fields["estimated_on"] == 1647170100
    assert normalize(FLIGHTPLAN).fields["estimated_on"] == 1589552338


def test_scalars_arrive_as_strings_and_become_ints():
    fields = normalize(FLIGHTPLAN).fields
    assert fields["estimated_off"] == 1589549400
    assert fields["scheduled_off"] == 1589548500
    assert isinstance(fields["estimated_on"], int)


def test_actual_arrival_beats_prediction():
    assert normalize(FLIGHTPLAN).best_eta() == ("predicted_on", 1589552000)
    assert normalize(ARRIVAL).best_eta() == ("actual_on", 1589549221)


def test_onblock_gate_arrival_comes_from_clock():
    assert normalize(ONBLOCK).best_eta() == ("actual_in", 1589549800)


def test_cancellation_is_flagged():
    message = {**FLIGHTPLAN, "type": "cancellation", "status": "X", "trueCancel": "1"}
    assert normalize(message).fields["cancelled"] == 1


def test_messages_without_flight_id_are_dropped():
    assert normalize({"type": "keepalive", "serverTime": "1589808417", "pitr": "1589808413"}) is None


def test_airline_code_handles_icao_and_numeric_iata():
    assert ident_airline("THA657") == "THA"
    assert ident_airline("8M501") == "8M"
    assert ident_airline("N104BA") is None


def test_configured_iata_codes_expand_to_icao_callsigns():
    prefixes = accepted_prefixes(("WE", "8M", "AS", "AA", "WS"))
    assert {"THA", "THD", "MMA", "ASA", "AAL", "WJA"} <= prefixes
    assert "WE" in prefixes
