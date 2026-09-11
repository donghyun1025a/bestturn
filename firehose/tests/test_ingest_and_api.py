from __future__ import annotations

import io
import json
from dataclasses import replace

from eta_ingest.api import build_handler
from eta_ingest.ingest import ingest
from eta_ingest.protocol import normalize
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


def call_api(store, settings, path: str) -> tuple[int, dict]:
    """소켓 없이 do_GET 만 불러 상태코드와 본문을 받습니다."""
    handler_class = build_handler(store, settings)
    handler = handler_class.__new__(handler_class)
    captured: dict[str, object] = {}
    handler.wfile = io.BytesIO()
    handler.send_response = lambda code, *a: captured.__setitem__("status", code)
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler.path = path
    handler.do_GET()
    return captured["status"], json.loads(handler.wfile.getvalue())


def test_flight_endpoint_returns_latest_eta(store, settings):
    ingest([FLIFO], store, settings)
    status, payload = call_api(store, settings, "/flight?ident=THA657")
    assert status == 200
    assert payload["flights"][0]["estimated_on"] == 1647170100


def test_flight_endpoint_requires_ident(store, settings):
    assert call_api(store, settings, "/flight")[0] == 400


def test_unknown_flight_is_404(store, settings):
    assert call_api(store, settings, "/flight?ident=ZZ999")[0] == 404


def test_history_endpoint_returns_the_eta_trail(store, settings):
    ingest([FLIFO, {**FLIFO, "estimated_on": "1647171000"}], store, settings)
    status, payload = call_api(store, settings, f"/history?flight_id={FLIFO['id']}")
    assert status == 200
    assert len(payload["eta_history"]) == 2


def test_arrivals_window_is_capped(store, settings):
    _, payload = call_api(store, settings, "/arrivals?hours=9999")
    assert payload["hours"] == 72
    assert payload["dest"] == "RKSI"


def test_normalize_is_shared_by_ingest_and_api(store, settings):
    assert normalize(FLIFO).flight_id == FLIFO["id"]
