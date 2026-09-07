import httpx
import pytest

from conftest import CONFIG_DIR, arrival_row, congestion_row, departure_row, envelope
from incheon_bot.api.client import IncheonAirportClient
from incheon_bot.api.models import Flight
from incheon_bot.bot import formatting as fmt
from incheon_bot.service import BriefingService


def make_service(handler):
    client = IncheonAirportClient("KEY", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return BriefingService(client, CONFIG_DIR)


def congestion_handler(waits, terminal="P01"):
    def handler(request):
        url = str(request.url)
        if "Congestion" in url:
            return httpx.Response(
                200, json=envelope([congestion_row(g, w, terminal) for g, w in waits.items()])
            )
        return httpx.Response(200, json=envelope([]))

    return handler


async def test_outbound_briefing_has_full_route():
    service = make_service(congestion_handler({"DG1_E": 42, "DG1_W": 7, "DG3_E": 5, "DG6_W": 2}))
    flight = Flight.from_raw(departure_row(), "OB")
    briefing = await service.build(flight)

    assert briefing.is_inbound is False
    assert briefing.route_origin == "체크인 카운터 A-B"
    assert briefing.best_route.gate.gate_id == "DG1_W"      # 가깝고 원활
    assert briefing.lounges is not None

    text = fmt.render_briefing(briefing, service.routing)
    for expected in ["체크인 카운터", "A-B", "보안심사대", "1번 출국장 (서)", "인근 출국장", "탑승구", "11", "라운지"]:
        assert expected in text, expected
    assert "OB 출국" in text and "T1" in text
    await service.client.aclose()


async def test_outbound_lounges_reflect_alliance_config():
    service = make_service(congestion_handler({"DG1_W": 5}))
    flight = Flight.from_raw(departure_row(flightId="OZ102", terminalId="P01", chkinRange="A-B"), "OB")
    briefing = await service.build(flight)
    names = {l.name for l in briefing.lounges.alliance_lounges + briefing.lounges.airline_lounges}
    assert briefing.lounges.alliance_name == "Star Alliance"
    assert "아시아나 비즈니스 라운지 (동편)" in names
    text = fmt.render_briefing(briefing, service.routing)
    assert "Star Alliance" in text and "마티나 라운지 (T1 동편)" in text   # 자사 계약 라운지도 함께
    await service.client.aclose()


async def test_inbound_briefing_lists_arrival_chain():
    service = make_service(congestion_handler({}))
    flight = Flight.from_raw(arrival_row(), "IB")
    briefing = await service.build(flight)

    assert briefing.is_inbound is True
    assert "T2 입국심사장" in briefing.immigration
    text = fmt.render_briefing(briefing, service.routing)
    for expected in ["IB 입국", "도착 게이트", "244", "입국심사대", "수하물 수취대", "7", "입국장 출구", "B"]:
        assert expected in text, expected
    assert "라운지" not in text          # 입국편에는 라운지/보안심사 섹션 없음
    assert "보안심사대" not in text
    await service.client.aclose()


async def test_inbound_pending_fields_produce_warnings():
    service = make_service(congestion_handler({}))
    flight = Flight.from_raw(arrival_row(carousel="-", exitNumber="-", remark="-"), "IB")
    briefing = await service.build(flight)
    joined = " ".join(briefing.warnings)
    assert "수하물 수취대" in joined and "출구" in joined
    assert "/book" in joined
    await service.client.aclose()


async def test_concourse_flight_gets_shuttle_note():
    service = make_service(congestion_handler({"DG1_W": 6, "DG6_E": 30}))
    flight = Flight.from_raw(departure_row(terminalId="P02", gateNumber="118", chkinRange="A-B"), "OB")
    briefing = await service.build(flight)
    assert briefing.transfer_note and "셔틀트레인" in briefing.transfer_note
    # 탑승동편도 T1 출국장 혼잡도로 동선을 계산해야 한다
    assert briefing.best_route.gate.gate_id == "DG1_W"
    assert "셔틀트레인" in fmt.render_briefing(briefing, service.routing)
    await service.client.aclose()


async def test_cargo_terminal_is_flagged():
    service = make_service(congestion_handler({}))
    flight = Flight.from_raw(departure_row(terminalId="C02", flightId="5Y8232"), "OB")
    briefing = await service.build(flight)
    assert briefing.route_options == []
    assert "화물터미널" in " ".join(briefing.warnings)
    await service.client.aclose()


async def test_congestion_failure_degrades_gracefully():
    def handler(request):
        if "Congestion" in str(request.url):
            return httpx.Response(200, text="API rate limit exceeded")
        return httpx.Response(200, json=envelope([]))

    service = make_service(handler)
    briefing = await service.build(Flight.from_raw(departure_row(), "OB"))
    assert "혼잡도" in " ".join(briefing.warnings)
    text = fmt.render_briefing(briefing, service.routing)
    assert "체크인 카운터" in text     # 나머지 정보는 그대로 제공
    await service.client.aclose()


async def test_choice_rendering_for_ambiguous_flight():
    service = make_service(congestion_handler({}))
    flights = [Flight.from_raw(departure_row(), "OB"), Flight.from_raw(arrival_row(flightId="WE501"), "IB")]
    text = fmt.render_choice(flights, service.routing)
    assert "OB 출발" in text and "IB 도착" in text
    await service.client.aclose()


async def test_headline_marks_delay_and_codeshare():
    service = make_service(congestion_handler({}))
    flight = Flight.from_raw(departure_row(codeshare="Slave", masterFlightId="TG659", remark="지연"), "OB")
    text = fmt.flight_headline(flight, service.routing)
    assert "18:00 → <b>18:15</b> (변경)" in text
    assert "코드셰어" in text and "TG659" in text
    assert "⏰" in text
    await service.client.aclose()


async def test_nearest_and_recommended_are_distinguished():
    """가장 가까운 출국장이 혼잡하면 추천은 달라지고, 그 사실을 문구로 알려준다."""
    service = make_service(congestion_handler({"DG1_E": 55, "DG1_W": 50, "DG2_E": 9, "DG5_W": 4}))
    flight = Flight.from_raw(departure_row(chkinRange="A", gateNumber="11"), "OB")
    briefing = await service.build(flight)

    assert briefing.nearest_route.gate.gate_id == "DG1_E"
    assert briefing.best_route.gate.gate_id == "DG2_E"
    text = fmt.render_briefing(briefing, service.routing)
    assert "최단거리는 1번 출국장 (동) 이나 대기가 길어" in text
    assert "인근 출국장 (가까운 순)" in text
    # 인근 목록은 거리순이므로 멀리 있는 5번보다 1번/2번이 앞에 온다
    nearby = [o.gate.gate_id for o in briefing.nearby(limit=20)]
    assert nearby.index("DG1_E") < nearby.index("DG5_W")
    await service.client.aclose()


async def test_lounge_section_skips_empty_alliance_group():
    """항공사 지정 라운지에 이미 나온 항목만 있으면 얼라이언스 머리글을 비워두지 않는다."""
    service = make_service(congestion_handler({"DG1_W": 5}))
    flight = Flight.from_raw(departure_row(flightId="OZ102", terminalId="P01"), "OB")
    briefing = await service.build(flight)
    text = fmt.render_briefing(briefing, service.routing)
    assert "<i>Star Alliance</i>" not in text      # 빈 얼라이언스 머리글이 남지 않아야 한다
    assert "아시아나 비즈니스 라운지 (동편)" in text
    assert text.count("아시아나 비즈니스 라운지 (동편)") == 1
    await service.client.aclose()
