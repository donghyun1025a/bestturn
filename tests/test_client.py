import httpx
import pytest

from conftest import congestion_row, departure_row, envelope
from incheon_bot.api.client import ApiError, BudgetExceeded, IncheonAirportClient

XML_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
<body><items><item><flightId>WE501</flightId><gateNumber>11</gateNumber>
<scheduleDateTime>202609071800</scheduleDateTime><terminalId>P01</terminalId></item></items>
<numOfRows>1</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount></body></response>"""


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return IncheonAirportClient("KEY", client=http, **kwargs)


async def test_find_flight_detects_direction():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "getFltDeparturesDeOdp" in str(request.url):
            return httpx.Response(200, json=envelope([departure_row()]))
        return httpx.Response(200, json=envelope([]))

    client = make_client(handler)
    flights = await client.find_flight("WE501", "20260907")
    assert [f.direction for f in flights] == ["OB"]
    assert flights[0].gate_number == "11"
    assert any("serviceKey=KEY" in c for c in calls)
    await client.aclose()


async def test_find_flight_returns_both_when_ambiguous():
    def handler(request):
        if "Departures" in str(request.url):
            return httpx.Response(200, json=envelope([departure_row()]))
        return httpx.Response(200, json=envelope([departure_row(terminalId="P03", carousel="7")]))

    client = make_client(handler)
    flights = await client.find_flight("WE501", "20260907")
    assert sorted(f.direction for f in flights) == ["IB", "OB"]
    await client.aclose()


async def test_falls_back_to_whole_day_when_server_filter_misses():
    """flightId 필터가 빈 결과를 주면 당일 전체 목록에서 다시 찾습니다."""
    def handler(request):
        if "Arrivals" in str(request.url):
            return httpx.Response(200, json=envelope([]))
        if "flightId=" in str(request.url):
            return httpx.Response(200, json=envelope([]))
        return httpx.Response(200, json=envelope([departure_row(), departure_row(flightId="KE86")]))

    client = make_client(handler)
    flights = await client.find_flight("WE 501", "20260907")
    assert len(flights) == 1 and flights[0].flight_id == "WE501"
    await client.aclose()


async def test_xml_response_is_parsed():
    client = make_client(lambda r: httpx.Response(200, text=XML_BODY))
    flights = await client.fetch_flights("OB", "20260907")
    assert flights[0].flight_id == "WE501"
    await client.aclose()


async def test_plaintext_portal_error_is_raised():
    client = make_client(lambda r: httpx.Response(200, text="Unauthorized"))
    with pytest.raises(ApiError, match="Unauthorized"):
        await client.fetch_flights("OB", "20260907")
    await client.aclose()


async def test_result_code_error_is_raised():
    payload = {"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}, "body": {}}}
    client = make_client(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ApiError, match="30"):
        await client.fetch_flights("OB", "20260907")
    await client.aclose()


async def test_single_item_dict_shape():
    payload = envelope([])
    payload["response"]["body"]["items"] = {"item": departure_row()}
    client = make_client(lambda r: httpx.Response(200, json=payload))
    flights = await client.fetch_flights("OB", "20260907")
    assert len(flights) == 1
    await client.aclose()


async def test_results_are_cached_within_ttl():
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json=envelope([departure_row()]))

    client = make_client(handler)
    await client.fetch_flights("OB", "20260907")
    await client.fetch_flights("OB", "20260907")
    assert len(hits) == 1
    assert client.flight_budget.used == 1
    await client.aclose()


async def test_daily_budget_is_enforced():
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        # 캐시를 피하기 위해 매번 다른 날짜로 호출합니다
        return httpx.Response(200, json=envelope([departure_row()]))

    client = make_client(handler, flight_daily_budget=2)
    await client.fetch_flights("OB", "20260901")
    await client.fetch_flights("OB", "20260902")
    with pytest.raises(BudgetExceeded):
        await client.fetch_flights("OB", "20260903")
    assert counter["n"] == 2
    await client.aclose()


async def test_congestion_t1_and_t2_endpoints():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=envelope([congestion_row("DG1_E", 12)]))

    client = make_client(handler)
    t1 = await client.fetch_congestion("T1")
    t2 = await client.fetch_congestion("T2")
    assert t1[0].wait_time_min == 12 and t2[0].gate_id == "DG1_E"
    assert any("statusOfDepartureCongestion/getDepartureCongestion" in u for u in seen)
    assert any("statusOfDepartureCongestionT2/getDepartureCongestionT2" in u for u in seen)
    assert client.congestion_budget.used == 2
    await client.aclose()


async def test_retries_on_server_error(monkeypatch):
    from incheon_bot.api import client as client_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json=envelope([departure_row()]))

    client = make_client(handler)
    flights = await client.fetch_flights("OB", "20260907")
    assert attempts["n"] == 3 and len(flights) == 1
    await client.aclose()
