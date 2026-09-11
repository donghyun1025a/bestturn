from __future__ import annotations

from dataclasses import replace

import pytest
from eta_ingest.api import Api
from eta_ingest.ingest import ingest
from test_protocol import ARRIVAL, FLIFO, ONBLOCK


def test_target_airline_arriving_at_icn_is_stored(store, settings):
    assert ingest([FLIFO], store, settings) == 1
    assert store.by_ident("THA657")[0]["eta"] == 1647170100


def test_other_airlines_and_other_destinations_are_skipped(store, settings):
    messages = [
        {**FLIFO, "ident": "KAL901", "id": "KAL901-1"},        # 대상 항공사 아님
        {**FLIFO, "dest": "VTBS", "id": "THA657-outbound"},    # ICN 도착편 아님
    ]
    assert ingest(messages, store, settings) == 0


def test_configured_airlines_are_honoured(store, settings):
    assert ingest([FLIFO], store, replace(settings, airlines=("AA",))) == 0


def test_eta_history_records_each_change_and_final_actual_time(store, settings):
    ingest([FLIFO, {**FLIFO, "estimated_on": "1647171000"}, {**FLIFO, "actual_on": "1647171234"}], store, settings)

    history = store.history(FLIFO["id"])

    assert [(h["source"], h["eta"]) for h in history] == [
        ("estimated_on", 1647170100),
        ("estimated_on", 1647171000),
        ("actual_on", 1647171234),
    ]


def test_unchanged_eta_does_not_add_history(store, settings):
    ingest([FLIFO, FLIFO, FLIFO], store, settings)
    assert len(store.history(FLIFO["id"])) == 1


def test_gate_arrival_overrides_runway_arrival(store, settings):
    ingest([ARRIVAL, ONBLOCK], store, settings)
    flight = store.flight(ARRIVAL["id"])
    assert (flight["eta_source"], flight["eta"]) == ("actual_in", 1589549800)


def test_pitr_is_persisted_for_resume(store, settings):
    ingest([{**FLIFO, "id": f"THA657-{i}", "pitr": str(1000 + i)} for i in range(100)], store, settings)
    assert store.get_pitr() == 1099


# ------------------------------------------------------------------------ API
@pytest.fixture
def api(store, settings) -> Api:
    return Api(store, settings)


def test_flight_endpoint_returns_latest_eta(api, store, settings):
    ingest([FLIFO], store, settings)
    status, payload = api.get("/api/flight", {"ident": ["THA657"]})
    assert status == 200
    assert payload["flights"][0]["estimated_on"] == 1647170100


def test_flight_endpoint_requires_ident(api):
    assert api.get("/api/flight", {})[0] == 400


def test_unknown_flight_is_404(api):
    assert api.get("/api/flight", {"ident": ["ZZ999"]})[0] == 404


def test_history_endpoint_returns_the_eta_trail(api, store, settings):
    ingest([FLIFO, {**FLIFO, "estimated_on": "1647171000"}], store, settings)
    status, payload = api.get("/api/history", {"flight_id": [FLIFO["id"]]})
    assert status == 200
    assert len(payload["eta_history"]) == 2


def test_arrivals_window_is_capped(api):
    _, payload = api.get("/api/arrivals", {"hours": ["9999"]})
    assert payload["hours"] == 72
    assert payload["dest"] == "RKSI"


def test_unknown_path_is_404(api):
    assert api.get("/api/nope", {})[0] == 404


def test_arrivals_reports_what_is_stored_so_an_empty_window_can_explain_itself(api, store, settings):
    """체험 계정이 과거 데이터를 재생하면 창은 비지만 DB 에는 쌓입니다."""
    ingest([FLIFO], store, settings)  # 2022년 편 — 지금 조회 범위 밖

    _, payload = api.get("/api/arrivals", {"hours": ["12"]})

    assert payload["flights"] == []
    assert payload["stored"]["total"] == 1
    assert payload["stored"]["first_eta"] == 1647170100


def test_stored_span_is_zero_before_anything_arrives(api):
    _, payload = api.get("/api/arrivals", {})
    assert payload["stored"]["total"] == 0


def test_compare_needs_a_service_key(api):
    status, payload = api.get("/api/compare", {})
    assert status == 400
    assert "서비스키" in payload["error"]


def test_ingest_start_refuses_without_credentials(store, settings):
    api = Api(store, replace(settings, username="", password=""))
    status, payload = api.post("/api/ingest/start", {})
    assert status == 400
    assert "API Key" in payload["error"]
