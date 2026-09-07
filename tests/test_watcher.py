from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from conftest import CONFIG_DIR, arrival_row, congestion_row, departure_row, envelope
from incheon_bot.api.client import IncheonAirportClient
from incheon_bot.api.models import Flight
from incheon_bot.service import BriefingService
from incheon_bot.storage.db import BookingStore
from incheon_bot.watcher import watch_tick


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def make_context(tmp_path, handler):
    client = IncheonAirportClient("KEY", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    service = BriefingService(client, CONFIG_DIR)
    store = BookingStore(tmp_path / "bookings.db")
    bot_ctx = SimpleNamespace(service=service, store=store)
    bot = FakeBot()
    context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={"ctx": bot_ctx}))
    return context, bot_ctx, bot


def book(store, flight: Flight, *, chat_id=100, when=None):
    return store.upsert(
        chat_id=chat_id,
        user_id=1,
        flight_key=flight.key,
        fid=flight.fid,
        flight_no=flight.flight_id,
        direction=flight.direction,
        search_date="20260907",
        label="OB",
        snapshot=flight.watched_fields(),
        next_check_at=when or datetime.now() - timedelta(minutes=1),
    )[0]


async def test_gate_change_triggers_notification(tmp_path):
    state = {"row": departure_row(gateNumber="11", chkinRange="A-B")}

    def handler(request):
        url = str(request.url)
        if "Congestion" in url:
            return httpx.Response(200, json=envelope([congestion_row("DG5_W", 6), congestion_row("DG1_W", 40)]))
        if "Departures" in url:
            return httpx.Response(200, json=envelope([state["row"]]))
        return httpx.Response(200, json=envelope([]))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    booking = book(bot_ctx.store, Flight.from_raw(state["row"], "OB"))

    await watch_tick(context)
    assert bot.sent == []                       # 변동 없으면 조용히 넘어감

    state["row"] = departure_row(gateNumber="42", chkinRange="K-L", remark="탑승준비")
    bot_ctx.service.client._cache.clear()
    bot_ctx.store.reschedule(booking.id, datetime.now() - timedelta(minutes=1))

    await watch_tick(context)
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 100
    assert "탑승구" in text and "42" in text
    assert "체크인 카운터" in text and "K-L" in text
    assert "탑승준비" in text
    assert "추천 출국장" in text                 # 동선 변동 시 재추천 동봉
    await bot_ctx.service.client.aclose()


async def test_inbound_carousel_assignment_notifies(tmp_path):
    state = {"row": arrival_row(carousel="-", exitNumber="-", remark="접근중")}

    def handler(request):
        if "Arrivals" in str(request.url):
            return httpx.Response(200, json=envelope([state["row"]]))
        return httpx.Response(200, json=envelope([]))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    booking = book(bot_ctx.store, Flight.from_raw(state["row"], "IB"))

    state["row"] = arrival_row(carousel="7", exitNumber="B", remark="착륙")
    bot_ctx.service.client._cache.clear()
    await watch_tick(context)

    text = bot.sent[0][1]
    assert "수하물 수취대" in text and "7" in text
    assert "입국장 출구" in text and "B" in text
    await bot_ctx.service.client.aclose()


async def test_watch_auto_closes_after_completion(tmp_path):
    row = departure_row(remark="출발", scheduleDateTime="202001010800", estimatedDateTime="202001010800")

    def handler(request):
        if "Departures" in str(request.url):
            return httpx.Response(200, json=envelope([row]))
        return httpx.Response(200, json=envelope([]))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    book(bot_ctx.store, Flight.from_raw(row, "OB"))

    await watch_tick(context)
    assert bot_ctx.store.list_active(100) == []
    assert any("자동 종료" in text for _, text in bot.sent)
    await bot_ctx.service.client.aclose()


async def test_done_stops_updates(tmp_path):
    row = departure_row()

    def handler(request):
        return httpx.Response(200, json=envelope([row] if "Departures" in str(request.url) else []))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    booking = book(bot_ctx.store, Flight.from_raw(row, "OB"))
    bot_ctx.store.close(booking.id)

    await watch_tick(context)
    assert bot.sent == []
    assert bot_ctx.store.due() == []
    await bot_ctx.service.client.aclose()


async def test_api_failure_reschedules_without_closing(tmp_path):
    def handler(request):
        return httpx.Response(200, text="Error receiving response from backend server")

    context, bot_ctx, bot = make_context(tmp_path, handler)
    booking = book(bot_ctx.store, Flight.from_raw(departure_row(), "OB"))

    await watch_tick(context)
    assert bot.sent == []
    still_active = bot_ctx.store.list_active(100)
    assert len(still_active) == 1
    assert still_active[0].next_check_at > datetime.now()
    await bot_ctx.service.client.aclose()


async def test_budget_exhaustion_defers_the_batch(tmp_path):
    def handler(request):
        return httpx.Response(200, json=envelope([departure_row()]))

    client = IncheonAirportClient(
        "KEY", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), flight_daily_budget=0
    )
    service = BriefingService(client, CONFIG_DIR)
    store = BookingStore(tmp_path / "b.db")
    bot = FakeBot()
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"ctx": SimpleNamespace(service=service, store=store)}),
    )
    booking = book(store, Flight.from_raw(departure_row(), "OB"))

    await watch_tick(context)
    assert bot.sent == []
    assert store.list_active(100)[0].next_check_at > datetime.now() + timedelta(minutes=20)
    await client.aclose()
