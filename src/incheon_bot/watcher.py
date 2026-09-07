"""book 등록 항공편의 변동을 주기적으로 확인하고 알림을 보냅니다."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from .api.client import ApiError, BudgetExceeded
from .bot import formatting as fmt
from .domain.reminders import custom_reminder_due
from .domain.status import adaptive_interval_minutes, diff_flights, should_stop_watching
from .api.models import normalize_flight_no
from .storage.db import Booking

log = logging.getLogger(__name__)

WATCH_TICK_SECONDS = 60
MAX_PER_TICK = 20


async def watch_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = context.application.bot_data["ctx"]
    store = bot_ctx.store
    service = bot_ctx.service
    due = store.due(limit=MAX_PER_TICK)
    if not due:
        return
    log.debug("감시 대상 %d건 확인", len(due))
    for booking in due:
        try:
            await _process(context, bot_ctx, booking)
        except BudgetExceeded as exc:
            log.warning("일일 호출 한도 소진, 다음 주기로 미룹니다: %s", exc)
            store.reschedule(booking.id, datetime.now() + timedelta(minutes=30))
            break
        except ApiError as exc:
            log.warning("[%s] 갱신 실패: %s", booking.flight_no, exc)
            store.reschedule(booking.id, datetime.now() + timedelta(minutes=5))
        except Exception:  # 한 건의 실패가 전체 루프를 멈추지 않도록
            log.exception("[%s] 감시 처리 중 오류", booking.flight_no)
            store.reschedule(booking.id, datetime.now() + timedelta(minutes=10))


async def _process(context: ContextTypes.DEFAULT_TYPE, bot_ctx, booking: Booking) -> None:
    store = bot_ctx.store
    service = bot_ctx.service

    stub = _stub_flight(booking)
    flight = await service.client.refresh_flight(stub, booking.search_date)
    if flight is None:
        # 운항 정보가 사라진 경우(취소/스케줄 삭제) — 몇 차례 더 시도한 뒤 종료
        store.reschedule(booking.id, datetime.now() + timedelta(minutes=15))
        return

    current = flight.watched_fields()
    changes = diff_flights(booking.snapshot, current)
    interval = adaptive_interval_minutes(flight)
    next_check = datetime.now() + timedelta(minutes=interval)
    notes = store.notes(booking.chat_id, normalize_flight_no(booking.flight_no), booking.search_date)

    await _fire_reminders(context, bot_ctx, booking, flight, notes)

    if changes:
        text = fmt.render_booking_update(flight, changes, service.routing)
        if not flight.is_inbound and any(c.field in {"탑승구", "체크인 카운터", "터미널"} for c in changes):
            briefing = await service.build(flight)
            best = briefing.best_route
            if best is not None:
                text += "\n\n" + fmt._route_line(best, service.routing, marker="🚦 추천 출국장: ")
        await _send(context, booking, text)

    store.update_snapshot(booking.id, current, next_check)

    if should_stop_watching(flight):
        store.close(booking.id)
        await _send(
            context,
            booking,
            f"✅ <b>{flight.flight_id}</b> 운항이 종료되어 감시를 자동 종료했습니다. (최종 현황: {flight.remark or '-'})",
        )


async def _fire_reminders(context, bot_ctx, booking: Booking, flight, notes: list) -> None:
    """설정 기반 사전 알림 + 사용자 지정 알림 발송.

    지연으로 예정시각이 바뀌면 아직 보내지 않은 알림은 새 시각 기준으로 자동 재계산됩니다.
    """
    service = bot_ctx.service
    store = bot_ctx.store

    due, expired = service.reminders.evaluate(flight, booking.sent_reminders)
    for reminder in due:
        extra = ""
        if reminder.include_route and not flight.is_inbound:
            briefing = await service.build(flight)
            best = briefing.best_route
            if best is not None:
                extra = fmt._route_line(best, service.routing, marker="🚦 추천 출국장: ")
        await _send(
            context,
            booking,
            fmt.render_reminder(flight, reminder.title, reminder.body, service.routing, extra=extra, notes=notes),
        )
    if due or expired:
        store.mark_reminders_sent(booking.id, [r.key for r in due] + expired)

    catch_up = service.reminders.catch_up_minutes
    for custom in store.reminders(
        booking.chat_id, normalize_flight_no(booking.flight_no), booking.search_date, pending_only=True
    ):
        if not custom_reminder_due(custom.minutes_before, flight, catch_up_minutes=catch_up):
            # 시기를 완전히 놓친 알림은 다시 울리지 않도록 발송 완료 처리
            reference = flight.best_dt
            if reference is not None and datetime.now() > reference - timedelta(
                minutes=custom.minutes_before
            ) + timedelta(minutes=catch_up):
                store.mark_reminder_sent(custom.id)
            continue
        offset = f"T-{custom.minutes_before}분" if custom.minutes_before >= 0 else f"T+{abs(custom.minutes_before)}분"
        await _send(
            context,
            booking,
            fmt.render_reminder(flight, f"지정 알림 ({offset})", custom.text, service.routing, notes=notes),
        )
        store.mark_reminder_sent(custom.id)


def _stub_flight(booking: Booking):
    """DB 에 저장된 최소 정보로 갱신 조회용 Flight 를 구성."""
    from .api.models import Flight

    return Flight.from_raw(
        {"fid": booking.fid or "", "flightId": booking.flight_no}, booking.direction
    )


async def _send(context: ContextTypes.DEFAULT_TYPE, booking: Booking, text: str) -> None:
    try:
        await context.bot.send_message(booking.chat_id, text, parse_mode=ParseMode.HTML)
    except Forbidden:
        log.warning("chat %s 로 전송 불가(차단됨). 감시를 종료합니다.", booking.chat_id)
        context.application.bot_data["ctx"].store.close(booking.id)
    except TelegramError as exc:
        log.warning("알림 전송 실패(chat %s): %s", booking.chat_id, exc)
