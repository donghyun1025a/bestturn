"""UI 가 쓰는 설정 저장과 공공데이터 대조."""
from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from eta_ingest import config
from eta_ingest.api import Api
from eta_ingest.compare import compare
from eta_ingest.iia import IiaArrival, IiaClient, IiaError, _epoch
from eta_ingest.runner import IngestRunner

# 2022-03-13 16:35 KST == 1647156900
IIA_ROW = {
    "flightId": "WE 0657", "masterFlightId": "TG657", "airline": "타이항공",
    "airport": "방콕", "scheduleDateTime": "202203131635", "estimatedDatetime": "202203131650",
    "gateNumber": "24", "carousel": "7", "exitNumber": "A", "terminalId": "T1",
    "aircraftRegNo": "HS-TBA", "remark": "지연",
}
FIREHOSE_ROW = {
    "flight_id": "THA657-1", "ident": "THA657", "reg": "HSTBA", "orig": "VTBS",
    "eta": 1647157500, "eta_source": "estimated_on", "scheduled_on": 1647156900,
    "estimated_arrival_gate": "25", "baggage_claim": "7", "cancelled": 0,
}


# ---------------------------------------------------------------------- 설정
def test_saved_config_overrides_environment(store, settings):
    config.save(store, {"firehose_username": "kim", "airlines": "KE, OZ", "airport": "gmp"})

    merged = config.effective(settings, store)

    assert merged.username == "kim"
    assert merged.airlines == ("KE", "OZ")
    assert merged.airport == "GMP"


def test_blank_values_do_not_wipe_a_saved_password(store, settings):
    config.save(store, {"firehose_password": "secret"})
    config.save(store, {"firehose_password": "   ", "firehose_username": "kim"})

    assert config.effective(settings, store).password == "secret"


def test_secrets_never_leave_the_server(store, settings):
    config.save(store, {"firehose_password": "secret", "data_go_kr_key": "abc"})

    view = config.public_view(config.effective(settings, store))

    assert view == {
        "firehose_username": "u",
        "has_firehose_password": True,
        "has_data_go_kr_key": True,
        "airlines": ["WE", "8M", "AS", "AA", "WS"],
        "airport": "ICN",
        "default_airlines": ["WE", "8M", "AS", "AA", "WS"],
    }
    assert "secret" not in str(view) and "abc" not in str(view)


def test_config_endpoint_round_trips_through_the_api(store, settings):
    api = Api(store, settings)
    status, payload = api.post("/api/config", {"firehose_username": "kim", "firehose_password": "pw"})

    assert status == 200
    assert payload["config"]["firehose_username"] == "kim"
    assert "pw" not in str(payload)


# ------------------------------------------------------------- 공공데이터 파싱
def test_kst_timestamps_become_epochs_comparable_with_firehose():
    assert _epoch("202203131635") == 1647156900


def test_broken_hour_2400_is_clamped():
    assert _epoch("202203132400") == _epoch("202203132359")


def test_key_casing_differences_are_absorbed():
    arrival = IiaArrival.from_raw(IIA_ROW)
    assert arrival.scheduled == 1647156900   # scheduleDateTime
    assert arrival.estimated == 1647157800   # estimatedDatetime


def test_flight_numbers_are_normalized():
    assert IiaArrival.from_raw(IIA_ROW).flight_no == "WE657"


def test_plain_text_portal_errors_are_reported():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="SERVICE KEY IS NOT REGISTERED ERROR."))
    client = IiaClient("key", client=httpx.Client(transport=transport))
    with pytest.raises(IiaError, match="SERVICE KEY"):
        client.arrivals("20220313")


def test_result_code_errors_are_reported():
    body = {"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE KEY IS NOT REGISTERED"}}}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    client = IiaClient("key", client=httpx.Client(transport=transport))
    with pytest.raises(IiaError, match="30"):
        client.arrivals("20220313")


def test_arrivals_are_cached_to_protect_the_daily_quota():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"response": {"body": {"items": {"item": [IIA_ROW]}}}})

    client = IiaClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.arrivals("20220313")
    client.arrivals("20220313")

    assert len(calls) == 1


# -------------------------------------------------------------------- 대조
def test_registration_is_the_strongest_match():
    [result] = compare([FIREHOSE_ROW], [IiaArrival.from_raw(IIA_ROW)])

    assert result["matched_by"] == "reg"
    assert result["diff_minutes"] == -5  # Firehose 가 공공데이터보다 5분 이르게 봄


def test_icao_callsign_matches_iata_flight_number_when_registration_is_missing():
    row = {**FIREHOSE_ROW, "reg": None}
    [result] = compare([row], [IiaArrival.from_raw({**IIA_ROW, "aircraftRegNo": ""})])

    assert result["matched_by"] == "flight_no"  # THA657 → WE657


def test_unmatched_flight_is_reported_rather_than_dropped():
    [result] = compare([FIREHOSE_ROW], [])

    assert result["matched_by"] is None
    assert result["iia"] is None
    assert result["diff_minutes"] is None


# ------------------------------------------------------------------ 수집 제어
def test_runner_reports_error_instead_of_dying_silently(store, settings):
    class Boom:
        last_pitr = None

        def stream(self):
            raise OSError("connection refused")

    runner = IngestRunner(store, build_client=lambda _: Boom())
    runner.start(settings)
    runner._thread.join(timeout=5)

    assert runner.running is False
    assert "connection refused" in runner.last_error


def test_runner_stores_only_target_flights(store, settings):
    from test_protocol import FLIFO

    class Fake:
        last_pitr = None

        def stream(self):
            yield FLIFO
            yield {**FLIFO, "ident": "KAL901", "id": "KAL901-1"}

    runner = IngestRunner(store, build_client=lambda _: Fake())
    runner.start(settings)
    runner._thread.join(timeout=5)

    assert (runner.messages, runner.stored) == (2, 1)


def test_runner_refuses_to_start_twice(store, settings):
    class Idle:
        last_pitr = None

        def stream(self):
            import time

            time.sleep(0.5)
            return iter(())

    runner = IngestRunner(store, build_client=lambda _: Idle())
    assert runner.start(settings) is True
    assert runner.start(settings) is False
    runner._thread.join(timeout=5)
