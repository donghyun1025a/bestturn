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


async def test_pre_alert_is_sent_with_memo_and_route(tmp_path, monkeypatch):
    """T-2h 사전 알림에는 추천 출국장과 해당 편 메모가 함께 나간다."""
    import incheon_bot.domain.reminders as reminders_module

    row = departure_row(scheduleDateTime="202609071800", estimatedDateTime="202609071800")

    def handler(request):
        if "Congestion" in str(request.url):
            return httpx.Response(200, json=envelope([congestion_row("DG1_W", 7), congestion_row("DG6_E", 50)]))
        return httpx.Response(200, json=envelope([row] if "Departures" in str(request.url) else []))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    flight = Flight.from_raw(row, "OB")
    booking = book(bot_ctx.store, flight)
    bot_ctx.store.add_note(
        chat_id=100, flight_no="WE501", search_date="20260907", text="VIP 3명 · 휠체어 1대", author="김의전"
    )

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 16, 5)

    monkeypatch.setattr(reminders_module, "datetime", FrozenDatetime)
    await watch_tick(context)

    alerts = [t for _, t in bot.sent if "⏰" in t]
    assert len(alerts) == 1
    text = alerts[0]
    assert "보안심사 진입 권장" in text
    assert "추천 출국장" in text and "1번 출국장 (서)" in text
    assert "VIP 3명 · 휠체어 1대" in text          # 메모 동봉
    assert "카운터 A-B" in text and "탑승구 11" in text

    # 같은 알림이 다음 주기에 다시 나가지 않는다
    assert bot_ctx.store.list_active(100)[0].sent_reminders == ["checkin", "meet", "security"]
    bot.sent.clear()
    bot_ctx.store.reschedule(booking.id, datetime.now() - timedelta(minutes=1))
    bot_ctx.service.client._cache.clear()
    await watch_tick(context)
    assert [t for _, t in bot.sent if "⏰" in t] == []
    await bot_ctx.service.client.aclose()


async def test_custom_reminder_fires_once(tmp_path, monkeypatch):
    import incheon_bot.domain.reminders as reminders_module
    import incheon_bot.watcher as watcher_module

    row = departure_row(scheduleDateTime="202609071800", estimatedDateTime="202609071800")

    def handler(request):
        if "Congestion" in str(request.url):
            return httpx.Response(200, json=envelope([congestion_row("DG1_W", 7)]))
        return httpx.Response(200, json=envelope([row] if "Departures" in str(request.url) else []))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    flight = Flight.from_raw(row, "OB")
    booking = book(bot_ctx.store, flight)
    bot_ctx.store.mark_reminders_sent(booking.id, ["checkin", "meet", "security", "gate", "boarding"])
    bot_ctx.store.add_reminder(
        chat_id=100, flight_no="WE501", search_date="20260907", minutes_before=90, text="픽업 차량 배차 확인"
    )

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 16, 35)

    monkeypatch.setattr(reminders_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(watcher_module, "datetime", FrozenDatetime)
    await watch_tick(context)

    alerts = [t for _, t in bot.sent if "픽업 차량 배차 확인" in t]
    assert len(alerts) == 1 and "지정 알림 (T-90분)" in alerts[0]
    assert bot_ctx.store.reminders(100, "WE501", "20260907", pending_only=True) == []
    await bot_ctx.service.client.aclose()


async def test_exit_assignment_notifies_inbound(tmp_path):
    """입국심사대의 근거가 되는 출구가 배정되면 알림이 나간다."""
    state = {"row": arrival_row(exitNumber="-", carousel="-", remark="접근중")}

    def handler(request):
        return httpx.Response(200, json=envelope([state["row"]] if "Arrivals" in str(request.url) else []))

    context, bot_ctx, bot = make_context(tmp_path, handler)
    book(bot_ctx.store, Flight.from_raw(state["row"], "IB"))

    state["row"] = arrival_row(exitNumber="A", carousel="12", remark="착륙")
    bot_ctx.service.client._cache.clear()
    await watch_tick(context)

    text = next(t for _, t in bot.sent if "변동 알림" in t)
    assert "입국장 출구" in text and "A" in text
    await bot_ctx.service.client.aclose()
