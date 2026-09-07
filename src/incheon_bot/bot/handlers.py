"""텔레그램 명령/메시지 핸들러."""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from html import escape as esc

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..api.client import ApiError
from ..api.models import Flight, normalize_flight_no
from ..domain.query import QueryError, looks_like_flight_query, parse_query
from ..domain.status import adaptive_interval_minutes
from ..service import BriefingService
from ..settings import Settings
from ..storage.db import BookingStore
from . import formatting as fmt

log = logging.getLogger(__name__)

TOKEN_TTL = 1800.0  # 인라인 버튼 유효시간(초)


class TokenCache:
    """콜백 데이터 64byte 제한 때문에 조회 결과를 토큰으로 참조합니다."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, list[Flight], str]] = {}

    def put(self, flights: list[Flight], search_date: str) -> str:
        self._prune()
        token = uuid.uuid4().hex[:10]
        self._items[token] = (time.monotonic() + TOKEN_TTL, flights, search_date)
        return token

    def get(self, token: str) -> tuple[list[Flight], str] | None:
        self._prune()
        entry = self._items.get(token)
        if entry is None:
            return None
        return entry[1], entry[2]

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._items.items() if v[0] < now]:
            self._items.pop(key, None)


class BotContext:
    """핸들러가 공유하는 의존성 묶음."""

    def __init__(self, settings: Settings, service: BriefingService, store: BookingStore) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self.tokens = TokenCache()


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]


def _authorized(update: Update, bot_ctx: BotContext) -> bool:
    user = update.effective_user
    return bot_ctx.settings.is_allowed(user.id if user else None)


async def _deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "이 봇은 등록된 사용자만 사용할 수 있습니다. 관리자에게 사용자 ID 등록을 요청하세요.\n"
            f"내 사용자 ID: {update.effective_user.id if update.effective_user else '알 수 없음'}"
        )


def _notes_for(bot_ctx: BotContext, chat_id: int, flight: Flight, search_date: str) -> list:
    return bot_ctx.store.notes(chat_id, normalize_flight_no(flight.flight_id), search_date)


async def _render_with_notes(
    bot_ctx: BotContext, chat_id: int, flight: Flight, search_date: str
) -> str:
    briefing = await bot_ctx.service.build(flight)
    text = fmt.render_briefing(briefing, bot_ctx.service.routing)
    return text + fmt.render_notes(_notes_for(bot_ctx, chat_id, flight, search_date))


def _briefing_keyboard(token: str, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 새로고침", callback_data=f"rf|{token}|{index}"),
                InlineKeyboardButton("📌 book (자동 알림)", callback_data=f"bk|{token}|{index}"),
            ]
        ]
    )


# ----------------------------------------------------------------- 기본 명령
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    await update.effective_message.reply_text(fmt.HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# ------------------------------------------------------------------ 항공편 조회
async def _resolve_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *, book: bool) -> None:
    bot_ctx = _ctx(context)
    message = update.effective_message
    try:
        query = parse_query(text)
    except QueryError as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.HTML)
        return

    placeholder = await message.reply_text(f"🔎 {esc(query.flight_no)} ({query.day:%m/%d}) 조회 중…", parse_mode=ParseMode.HTML)
    try:
        flights = await bot_ctx.service.search(query.flight_no, query.search_date)
    except ApiError as exc:
        await placeholder.edit_text(f"❌ 조회 실패: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return

    if not flights:
        await placeholder.edit_text(
            f"<b>{esc(query.flight_no)}</b> ({query.day:%m/%d}) 운항 정보를 찾지 못했습니다.\n"
            "• 편명/날짜를 확인해 주세요 (여객편 기준)\n"
            "• 화물편은 <code>/cargo 편명</code> 으로 조회하세요",
            parse_mode=ParseMode.HTML,
        )
        return

    token = bot_ctx.tokens.put(flights, query.search_date)
    if len(flights) > 1:
        buttons = [
            [
                InlineKeyboardButton(
                    f"{'IB' if f.is_inbound else 'OB'} {f.best_dt:%m/%d %H:%M} "
                    f"{f.airport or ''}"[:60],
                    callback_data=f"{'bk' if book else 'pk'}|{token}|{i}",
                )
            ]
            for i, f in enumerate(flights)
            if f.best_dt
        ]
        await placeholder.edit_text(
            fmt.render_choice(flights, bot_ctx.service.routing),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )
        return

    flight = flights[0]
    if book:
        await placeholder.edit_text(
            await _do_book(update, context, flight, query.search_date), parse_mode=ParseMode.HTML
        )
        return
    await placeholder.edit_text(
        await _render_with_notes(bot_ctx, update.effective_chat.id, flight, query.search_date),
        parse_mode=ParseMode.HTML,
        reply_markup=_briefing_keyboard(token, 0),
    )


async def cmd_flight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    await _resolve_and_reply(update, context, " ".join(context.args or []), book=False)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        return
    text = (update.effective_message.text or "").strip()
    # 한글 키워드는 텔레그램 명령어 규칙(ASCII)상 /명령 으로 등록할 수 없어 일반 메시지로 처리합니다.
    head = text.split()[0] if text.split() else ""
    if head in {"혼잡도", "출국장"}:
        context.args = text.split()[1:]
        await cmd_congestion(update, context)
        return
    if head == "라운지":
        context.args = text.split()[1:]
        await cmd_lounge(update, context)
        return
    if head in {"완료", "종료"}:
        context.args = text.split()[1:]
        await cmd_done(update, context)
        return
    if head in {"메모", "노트"} and len(text.split()) > 1:
        context.args = text.split()[1:]
        await cmd_memo(update, context)
        return
    if head in {"알림", "리마인드"} and len(text.split()) > 1:
        context.args = text.split()[1:]
        await cmd_remind(update, context)
        return
    if head in {"예약", "감시"} and len(text.split()) > 1:
        await _resolve_and_reply(update, context, " ".join(text.split()[1:]), book=True)
        return
    if not looks_like_flight_query(text):
        return
    await _resolve_and_reply(update, context, text, book=False)


# ------------------------------------------------------------------- book/done
async def _do_book(update: Update, context: ContextTypes.DEFAULT_TYPE, flight: Flight, search_date: str) -> str:
    bot_ctx = _ctx(context)
    chat = update.effective_chat
    user = update.effective_user
    snapshot = {k: v for k, v in flight.watched_fields().items()}
    interval = adaptive_interval_minutes(flight)
    already_passed = bot_ctx.service.reminders.initial_sent_keys(flight)
    label = f"{'IB' if flight.is_inbound else 'OB'} {flight.best_dt:%m/%d %H:%M}" if flight.best_dt else flight.direction
    booking, created = bot_ctx.store.upsert(
        chat_id=chat.id,
        user_id=user.id if user else None,
        flight_key=flight.key,
        fid=flight.fid,
        flight_no=flight.flight_id.upper(),
        direction=flight.direction,
        search_date=search_date,
        label=label,
        snapshot=snapshot,
        next_check_at=datetime.now() + timedelta(minutes=interval),
        sent_reminders=already_passed,
    )
    verb = "등록" if created else "갱신"
    watched = (
        "도착 게이트 · 수하물 수취대 · 입국장 출구 · 현황"
        if flight.is_inbound
        else "체크인 카운터 · 탑승구 · 현황"
    )
    rules = bot_ctx.service.reminders.rules_for(flight.direction)
    pending = [r for r in rules if r.key not in already_passed]
    schedule = ", ".join(r.offset_label for r in pending) if pending else "없음(시각 경과)"
    notes = _notes_for(bot_ctx, chat.id, flight, search_date)
    lines = [
        f"📌 <b>{esc(flight.flight_id)}</b> ({esc(label)}) 감시를 {verb}했습니다.",
        f"변동 감시 항목: {watched} · 시각 변경",
        f"⏰ 사전 알림: {esc(schedule)}",
        f"다음 확인까지 약 {interval}분 · 종료: <code>/done {esc(flight.flight_id)}</code>",
    ]
    text = "\n".join(lines) + fmt.render_notes(notes)
    return text


async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "사용법: <code>/book WE501</code> 또는 <code>/book WE501 0908</code>", parse_mode=ParseMode.HTML
        )
        return
    await _resolve_and_reply(update, context, " ".join(context.args), book=True)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    chat_id = update.effective_chat.id
    args = [a.upper() for a in (context.args or [])]
    if not args:
        await update.effective_message.reply_text(
            "사용법: <code>/done WE501</code> · 전체 종료: <code>/done all</code>", parse_mode=ParseMode.HTML
        )
        return
    if args[0] in {"ALL", "전체"}:
        count = bot_ctx.store.close_all(chat_id)
        await update.effective_message.reply_text(f"✅ 감시 중이던 {count}건을 모두 종료했습니다.")
        return
    wanted = normalize_flight_no(args[0])
    matches = [b for b in bot_ctx.store.list_active(chat_id) if normalize_flight_no(b.flight_no) == wanted]
    if not matches:
        await update.effective_message.reply_text(f"{args[0]} 은(는) 감시 중이 아닙니다. /list 로 확인하세요.")
        return
    for booking in matches:
        bot_ctx.store.close(booking.id)
    await update.effective_message.reply_text(
        f"✅ <b>{esc(args[0])}</b> 감시를 종료했습니다. ({len(matches)}건)", parse_mode=ParseMode.HTML
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    bookings = bot_ctx.store.list_active(update.effective_chat.id)
    rows = [(b, b.label) for b in bookings]
    await update.effective_message.reply_text(fmt.render_booking_list(rows), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------- 부가기능
async def cmd_congestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    arg = (context.args[0].upper() if context.args else "T1").replace("터미널", "")
    terminal_code = "P03" if arg in {"T2", "2", "P03"} else "P01"
    congestion, warning = await bot_ctx.service.congestion_for(terminal_code)
    if warning or not congestion:
        await update.effective_message.reply_text(warning or "혼잡도 데이터가 없습니다.")
        return
    routing = bot_ctx.service.routing
    terminal = routing.terminal(terminal_code)
    lines = [f"<b>🚦 {esc(terminal.name if terminal else terminal_code)} 출국장 혼잡도</b>", ""]
    gates = terminal.departure_gates if terminal else {}
    for item in sorted(congestion, key=lambda c: c.gate_id):
        gate = gates.get(item.gate_id)
        label = gate.label if gate else item.gate_id
        name, emoji = routing.level_for(item.wait_time_min)
        if not item.is_open:
            lines.append(f"{esc(label)} — 미운영 {esc(item.operating_time or '')}")
            continue
        queue = f" / 대기 {item.wait_length}명" if item.wait_length is not None else ""
        lines.append(f"{emoji} {esc(label)} — <b>{esc(item.wait_time_raw or '')}분</b> ({esc(name)}{queue})")
    occur = next((c.occur_time for c in congestion if c.occur_time), None)
    if occur:
        lines += ["", f"<i>기준 시각 {occur:%H:%M}</i>"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_lounge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "사용법: <code>/lounge KE</code> (항공사 2자리 코드) 또는 <code>/lounge WE501</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    raw = context.args[0].upper()
    carrier = raw[:2] if raw[:2].isalnum() else raw
    suggestion = bot_ctx.service.lounges.suggest(carrier)
    header = f"<b>🛋 {esc(carrier)} 라운지</b>"
    if suggestion.alliance_name:
        header += f" · {esc(suggestion.alliance_name)}"
    body = "\n".join(fmt._lounge_section(suggestion)[1:])
    await update.effective_message.reply_text(f"{header}\n{body}", parse_mode=ParseMode.HTML)


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    try:
        bot_ctx.service.reload_config()
    except Exception as exc:  # 설정 파일 오류는 사용자에게 그대로 보여줍니다
        await update.effective_message.reply_text(f"❌ 설정 로드 실패: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    await update.effective_message.reply_text("✅ 라운지·동선 설정을 다시 읽었습니다.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    active = bot_ctx.store.list_active(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"<b>상태</b>\n"
        f"• 오늘 API 사용량: {esc(bot_ctx.service.client.budget_status())}\n"
        f"• 이 대화 감시 중: {len(active)}건\n"
        f"• 서버 시각: {datetime.now():%Y-%m-%d %H:%M}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """화물편 조회(기본 조회는 여객편 기준)."""
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    try:
        query = parse_query(" ".join(context.args or []))
    except QueryError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    found: list[Flight] = []
    for direction in ("IB", "OB"):
        try:
            flights = await bot_ctx.service.client.fetch_flights(
                direction, query.search_date, flight_id=query.flight_no, passenger_or_cargo="C"
            )
        except ApiError as exc:
            await update.effective_message.reply_text(f"❌ 조회 실패: {exc}")
            return
        found.extend(flights)
    if not found:
        await update.effective_message.reply_text(f"{query.flight_no} 화물편 정보를 찾지 못했습니다.")
        return
    routing = bot_ctx.service.routing
    await update.effective_message.reply_text(
        "\n\n".join(fmt.flight_headline(f, routing) for f in found), parse_mode=ParseMode.HTML
    )


# ----------------------------------------------------------------- 메모 / 알림
CLEAR_WORDS = {"삭제", "취소", "clear", "delete", "reset", "전체삭제"}


def _split_flight_arg(args: list[str]) -> tuple[str, list[str]]:
    """'/memo WE501 0908 내용...' 처럼 편명(+날짜) 뒤에 자유 텍스트가 오는 입력을 분리."""
    if not args:
        return "", []
    head = [args[0]]
    rest = args[1:]
    # 두 번째 토큰이 날짜처럼 보이면 편명 파싱에 함께 넘깁니다.
    if rest and re.fullmatch(r"\d{4}|\d{8}|\d{4}-\d{2}-\d{2}", rest[0]):
        head.append(rest[0])
        rest = rest[1:]
    elif rest and rest[0] in {"내일", "모레", "오늘", "어제"}:
        head.append(rest[0])
        rest = rest[1:]
    return " ".join(head), rest


async def cmd_memo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """메모 추가/조회/삭제. 항공편에 붙는 자유 메모로, book 여부와 무관하게 유지됩니다."""
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    message = update.effective_message
    args = context.args or []
    if not args:
        await message.reply_text(
            "사용법\n"
            "<code>/memo WE501 VIP 3명, 휠체어 1대</code> — 메모 추가\n"
            "<code>/memo WE501</code> — 메모 보기\n"
            "<code>/memo WE501 삭제</code> — 전체 삭제 · <code>/memo WE501 삭제 2</code> — 2번 삭제",
            parse_mode=ParseMode.HTML,
        )
        return

    flight_arg, rest = _split_flight_arg(args)
    try:
        query = parse_query(flight_arg)
    except QueryError as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    flight_no, search_date = query.flight_no, query.search_date

    if rest and rest[0].lower() in CLEAR_WORDS:
        if len(rest) > 1 and rest[1].isdigit():
            removed = bot_ctx.store.delete_note(chat_id, flight_no, search_date, int(rest[1]))
            if removed is None:
                await message.reply_text("해당 번호의 메모가 없습니다. <code>/memo 편명</code> 으로 확인하세요.", parse_mode=ParseMode.HTML)
                return
            await message.reply_text(f"🗑 메모를 삭제했습니다: {esc(removed.text)}", parse_mode=ParseMode.HTML)
            return
        count = bot_ctx.store.clear_notes(chat_id, flight_no, search_date)
        await message.reply_text(f"🗑 <b>{esc(flight_no)}</b> 메모 {count}건을 삭제했습니다.", parse_mode=ParseMode.HTML)
        return

    if rest:
        text = " ".join(rest).strip()
        user = update.effective_user
        author = (user.full_name if user else None) or None
        bot_ctx.store.add_note(
            chat_id=chat_id, flight_no=flight_no, search_date=search_date, text=text, author=author
        )
        notes = bot_ctx.store.notes(chat_id, flight_no, search_date)
        await message.reply_text(
            f"📝 <b>{esc(flight_no)}</b> ({query.day:%m/%d}) 메모를 저장했습니다."
            + fmt.render_notes(notes),
            parse_mode=ParseMode.HTML,
        )
        return

    notes = bot_ctx.store.notes(chat_id, flight_no, search_date)
    if not notes:
        await message.reply_text(
            f"<b>{esc(flight_no)}</b> ({query.day:%m/%d}) 에 저장된 메모가 없습니다.\n"
            "<code>/memo {0} 내용</code> 으로 추가하세요.".format(esc(flight_no)),
            parse_mode=ParseMode.HTML,
        )
        return
    await message.reply_text(
        f"<b>{esc(flight_no)}</b> ({query.day:%m/%d})" + fmt.render_notes(notes), parse_mode=ParseMode.HTML
    )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """사용자 지정 사전 알림. 지연으로 시각이 바뀌면 함께 밀립니다."""
    bot_ctx = _ctx(context)
    if not _authorized(update, bot_ctx):
        await _deny(update)
        return
    message = update.effective_message
    args = context.args or []
    if not args:
        await message.reply_text(
            "사용법\n"
            "<code>/remind WE501 90 픽업 차량 배차 확인</code> — 출발/도착 90분 전 알림\n"
            "<code>/remind WE501</code> — 등록된 알림 보기 · <code>/remind WE501 취소</code> — 전체 취소\n"
            "<i>기본 사전 알림(T-4h/T-3h/T-2h/T-1h/T-40m 등)은 /book 시 자동 적용됩니다.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    flight_arg, rest = _split_flight_arg(args)
    try:
        query = parse_query(flight_arg)
    except QueryError as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.HTML)
        return
    chat_id = update.effective_chat.id
    flight_no, search_date = query.flight_no, query.search_date

    if rest and rest[0].lower() in CLEAR_WORDS:
        count = bot_ctx.store.clear_reminders(chat_id, flight_no, search_date)
        await message.reply_text(f"🗑 <b>{esc(flight_no)}</b> 사용자 지정 알림 {count}건을 취소했습니다.", parse_mode=ParseMode.HTML)
        return

    if not rest:
        await message.reply_text(
            fmt.render_reminder_list(flight_no, bot_ctx.store.reminders(chat_id, flight_no, search_date)),
            parse_mode=ParseMode.HTML,
        )
        return

    offset_token = rest[0]
    match = re.fullmatch(r"(?i)([+-]?\d{1,4})\s*(분|m|min)?", offset_token)
    if not match:
        await message.reply_text(
            "몇 분 전인지 숫자로 입력해 주세요. 예) <code>/remind WE501 90 픽업 차량 확인</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    minutes = int(match.group(1))
    if not -720 <= minutes <= 1440:
        await message.reply_text("알림 시점은 -720분(도착 후 12시간) ~ 1440분(24시간 전) 범위로 지정해 주세요.")
        return
    text = " ".join(rest[1:]).strip() or "의전 담당자 지정 알림"
    reminder = bot_ctx.store.add_reminder(
        chat_id=chat_id, flight_no=flight_no, search_date=search_date,
        minutes_before=minutes, text=text,
    )
    offset = f"T-{minutes}분" if minutes >= 0 else f"T+{abs(minutes)}분"
    booked = bot_ctx.store.find(chat_id, flight_no)
    tail = "" if booked else f"\n⚠️ <code>/book {esc(flight_no)}</code> 로 감시를 등록해야 알림이 발송됩니다."
    await message.reply_text(
        f"⏰ <b>{esc(flight_no)}</b> {esc(offset)} 알림을 등록했습니다: {esc(reminder.text)}{tail}",
        parse_mode=ParseMode.HTML,
    )


# -------------------------------------------------------------------- 콜백버튼
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_ctx = _ctx(context)
    query = update.callback_query
    await query.answer()
    if not _authorized(update, bot_ctx):
        return
    try:
        action, token, raw_index = (query.data or "").split("|", 2)
        index = int(raw_index)
    except (ValueError, AttributeError):
        await query.edit_message_text("만료된 버튼입니다. 다시 조회해 주세요.")
        return
    entry = bot_ctx.tokens.get(token)
    if entry is None:
        await query.edit_message_text("만료된 버튼입니다. 다시 조회해 주세요.")
        return
    flights, search_date = entry
    if not 0 <= index < len(flights):
        await query.edit_message_text("만료된 버튼입니다. 다시 조회해 주세요.")
        return
    flight = flights[index]

    if action == "bk":
        await query.edit_message_text(
            await _do_book(update, context, flight, search_date), parse_mode=ParseMode.HTML
        )
        return

    if action == "rf":
        # 새로고침은 최신 스냅샷을 다시 조회
        try:
            refreshed = await bot_ctx.service.client.refresh_flight(flight, search_date)
        except ApiError as exc:
            await query.answer(f"조회 실패: {exc}", show_alert=True)
            return
        flight = refreshed or flight
        flights[index] = flight

    text = await _render_with_notes(bot_ctx, update.effective_chat.id, flight, search_date)
    if action == "rf":
        text += f"\n<i>업데이트 {datetime.now():%H:%M:%S}</i>"
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_briefing_keyboard(token, index)
        )
    except Exception as exc:  # 내용이 동일하면 텔레그램이 400 을 돌려줍니다
        log.debug("메시지 수정 실패: %s", exc)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["flight", "f"], cmd_flight))
    app.add_handler(CommandHandler("book", cmd_book))
    app.add_handler(CommandHandler(["done", "complete"], cmd_done))
    app.add_handler(CommandHandler(["list", "bookings"], cmd_list))
    app.add_handler(CommandHandler(["congestion", "cong"], cmd_congestion))
    app.add_handler(CommandHandler("lounge", cmd_lounge))
    app.add_handler(CommandHandler(["memo", "note"], cmd_memo))
    app.add_handler(CommandHandler(["remind", "reminder"], cmd_remind))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cargo", cmd_cargo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
