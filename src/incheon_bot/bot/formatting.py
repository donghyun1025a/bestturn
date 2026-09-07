"""텔레그램 HTML 파스모드 메시지 렌더링."""
from __future__ import annotations

from html import escape as esc

from ..api.models import Flight
from ..domain.lounges import Lounge, LoungeSuggestion
from ..domain.routing import RouteOption, RoutingConfig
from ..domain.status import status_emoji
from ..service import Briefing

DASH = "—"


def _t(value: str | None) -> str:
    return esc(value) if value else DASH


def _hhmm(flight: Flight) -> str:
    sched = flight.schedule_dt.strftime("%H:%M") if flight.schedule_dt else DASH
    if flight.estimated_dt and flight.schedule_dt and flight.estimated_dt != flight.schedule_dt:
        return f"{sched} → <b>{flight.estimated_dt:%H:%M}</b> (변경)"
    return sched


def flight_headline(flight: Flight, routing: RoutingConfig) -> str:
    terminal = routing.terminal(flight.terminal_id)
    term_label = terminal.short if terminal else (flight.terminal_id or DASH)
    tag = "IB 입국" if flight.is_inbound else "OB 출국"
    route = f"{esc(flight.airport or '')} ({esc(flight.airport_code or '')})".strip()
    arrow = f"{route} → ICN" if flight.is_inbound else f"ICN → {route}"
    day = flight.schedule_dt.strftime("%m/%d") if flight.schedule_dt else ""
    lines = [
        f"<b>{esc(flight.flight_id)}</b> · {tag} · {esc(term_label)}",
        f"{esc(flight.airline or '')} | {arrow}",
        f"🗓 {day} {'출발' if not flight.is_inbound else '도착'} {_hhmm(flight)}",
    ]
    if flight.remark:
        lines.append(f"{status_emoji(flight.remark)} 현황: <b>{esc(flight.remark)}</b>")
    if flight.codeshare and flight.codeshare.lower() == "slave" and flight.master_flight_id:
        lines.append(f"🔗 코드셰어(운항편: {esc(flight.master_flight_id)})")
    return "\n".join(lines)


def _lounge_block(lounge: Lounge) -> str:
    return (
        f"  · <b>{esc(lounge.name)}</b>\n"
        f"    {esc(lounge.location)} | {esc(lounge.hours)}\n"
        f"    이용: {esc(lounge.access)}"
    )


def _lounge_section(suggestion: LoungeSuggestion | None) -> list[str]:
    lines = ["", "<b>🛋 라운지</b>"]
    if suggestion is None or suggestion.is_empty:
        lines.append(f"  {DASH} 설정된 라운지가 없습니다. config/lounges.yml 을 확인하세요.")
        return lines
    seen: set[str] = set()

    def add_group(title: str, group: tuple[Lounge, ...]) -> None:
        fresh = [l for l in group if l.key not in seen]
        if not fresh:
            return
        lines.append(f"  <i>{esc(title)}</i>")
        for lounge in fresh:
            lines.append(_lounge_block(lounge))
            seen.add(lounge.key)

    add_group("항공사 지정", suggestion.airline_lounges)
    add_group(suggestion.alliance_name or "얼라이언스", suggestion.alliance_lounges)
    add_group("자사 계약", suggestion.contract_lounges)
    if len(lines) == 2:
        lines.append(f"  {DASH} 해당 터미널에 설정된 라운지가 없습니다. config/lounges.yml 을 확인하세요.")
    return lines


def _route_line(option: RouteOption, routing: RoutingConfig, *, marker: str = "  ") -> str:
    name, emoji = routing.level_for(option.wait_minutes)
    if option.congestion is None:
        return f"{marker}{esc(option.gate.label)} — 혼잡도 정보 없음 (도보 약 {option.walk_minutes:.0f}분)"
    if not option.congestion.is_open:
        hours = option.congestion.operating_time
        suffix = f" (운영시간 {esc(hours)})" if hours else ""
        return f"{marker}{esc(option.gate.label)} — 미운영{suffix}"
    wait = option.congestion.wait_time_raw or str(option.wait_minutes)
    queue = f", 대기 {option.congestion.wait_length}명" if option.congestion.wait_length is not None else ""
    return (
        f"{marker}{emoji} {esc(option.gate.label)} — 대기 <b>{esc(str(wait))}분</b> ({esc(name)}{queue})"
        f" · 도보 약 {option.walk_minutes:.0f}분 · 합계 약 {option.total_minutes:.0f}분"
    )


def render_briefing(briefing: Briefing, routing: RoutingConfig) -> str:
    flight = briefing.flight
    lines = [flight_headline(flight, routing), ""]

    if briefing.is_inbound:
        lines += [
            "<b>🛬 입국 동선 (IB)</b>",
            f"  1️⃣ 도착 게이트: <b>{_t(flight.gate_number)}</b>"
            + (f" (주기장 {esc(flight.stand_position)})" if flight.stand_position else ""),
            f"  2️⃣ 입국심사대: <b>{_t(briefing.immigration)}</b>",
            f"  3️⃣ 수하물 수취대: <b>{_t(flight.carousel)}</b>"
            + (f" · {esc(briefing.baggage_area)}" if briefing.baggage_area else ""),
            f"  4️⃣ 입국장 출구: <b>{_t(flight.exit_number)}</b>",
        ]
        if briefing.transfer_note:
            lines += ["", f"🚈 {esc(briefing.transfer_note)}"]
    else:
        lines += [
            "<b>🛫 출국 동선 (OB)</b>",
            f"  1️⃣ 체크인 카운터: <b>{_t(flight.checkin_range)}</b>",
        ]
        best = briefing.best_route
        if best is not None:
            lines.append(f"  2️⃣ 보안심사대(출국장) — 추천 <i>기준: {esc(briefing.route_origin)}</i>")
            lines.append(_route_line(best, routing, marker="     ⭐ "))
            nearest = briefing.nearest_route
            if nearest is not None and nearest is not best:
                lines.append(
                    f"     <i>최단거리는 {esc(nearest.gate.label)} 이나 대기가 길어 위를 추천합니다.</i>"
                )
            others = briefing.nearby()
            if others:
                lines.append("     <i>인근 출국장 (가까운 순)</i>")
                for option in others:
                    lines.append(_route_line(option, routing, marker="     · "))
        else:
            lines.append("  2️⃣ 보안심사대(출국장): 실시간 혼잡도 정보를 확인할 수 없습니다.")
        lines.append(f"  3️⃣ 탑승구: <b>{_t(flight.gate_number)}</b>")
        if briefing.transfer_note:
            lines.append(f"     🚈 {esc(briefing.transfer_note)}")
        lines += _lounge_section(briefing.lounges)

    if briefing.warnings:
        lines += [""] + [f"⚠️ {esc(w)}" for w in briefing.warnings]

    occur = next(
        (o.congestion.occur_time for o in briefing.route_options if o.congestion and o.congestion.occur_time),
        None,
    )
    if occur:
        lines += ["", f"<i>혼잡도 기준 시각 {occur:%H:%M}</i>"]
    return "\n".join(lines)


def render_choice(flights: list[Flight], routing: RoutingConfig) -> str:
    lines = ["동일 편명이 여러 건 조회되었습니다. 아래에서 선택하세요.", ""]
    for idx, flight in enumerate(flights, start=1):
        terminal = routing.terminal(flight.terminal_id)
        when = flight.best_dt.strftime("%m/%d %H:%M") if flight.best_dt else DASH
        tag = "IB 도착" if flight.is_inbound else "OB 출발"
        lines.append(
            f"{idx}. <b>{esc(flight.flight_id)}</b> {tag} {when} "
            f"{esc(terminal.short if terminal else flight.terminal_id or '')} "
            f"({esc(flight.airport or '')})"
        )
    return "\n".join(lines)


def render_booking_update(flight: Flight, changes: list, routing: RoutingConfig) -> str:
    header = f"🔔 <b>{esc(flight.flight_id)}</b> 변동 알림"
    terminal = routing.terminal(flight.terminal_id)
    sub = f"{'IB 도착' if flight.is_inbound else 'OB 출발'} · {esc(terminal.short if terminal else flight.terminal_id or '')} · {_hhmm(flight)}"
    body = "\n".join(change.render() for change in changes)
    return f"{header}\n{sub}\n\n{body}"


def render_booking_list(rows: list[tuple]) -> str:
    if not rows:
        return "감시 중인 항공편이 없습니다. <code>/book WE501</code> 로 등록하세요."
    lines = ["<b>📌 감시 중인 항공편</b>", ""]
    for booking, label in rows:
        lines.append(
            f"• <b>{esc(booking.flight_no)}</b> ({'IB' if booking.is_inbound else 'OB'}) "
            f"{esc(booking.search_date)} — {esc(label or '')}"
        )
    lines += ["", "완료 처리: <code>/done 편명</code> · 전체 종료: <code>/done all</code>"]
    return "\n".join(lines)


HELP_TEXT = """<b>인천공항 의전 정보 봇</b>

<b>조회</b>
<code>WE501</code> — 오늘 기준 자동 IB/OB 판별 후 동선 브리핑
<code>WE501 0908</code> / <code>WE501 내일</code> / <code>WE501 20260908</code>
<code>/flight WE501 0908</code> — 동일 (명령어 형태)

<b>감시(book)</b>
<code>/book WE501</code> — 게이트·카운터·수취대·현황 변동 자동 알림
<code>/list</code> — 감시 중인 항공편 목록
<code>/done WE501</code> — 해당 편 감시 종료 (<code>/done all</code> 전체 종료)

<b>기타</b>
<code>/congestion T1</code> (또는 <code>T2</code>) — 출국장 실시간 혼잡도 전체
<code>/lounge KE</code> — 항공사/얼라이언스 라운지
<code>/reload</code> — 라운지·동선 설정 파일 다시 읽기
<code>/status</code> — API 사용량·감시 현황

<b>한글 입력도 가능</b>
<code>혼잡도 T2</code> · <code>라운지 KE</code> · <code>감시 WE501</code> · <code>완료 WE501</code>

<b>안내</b>
• IB: 게이트 → 입국심사대 → 수하물 수취대 → 입국장 출구
• OB: 체크인 카운터 → 보안심사대(실시간 혼잡도 기반 최적 동선) → 라운지 → 탑승구
• 운항 정보는 조회일 기준 -3일 ~ +6일까지 제공됩니다."""
